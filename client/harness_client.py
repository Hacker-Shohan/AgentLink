#!/usr/bin/env python3
"""
AI Agent PC Harness — client.

Connects outbound to the VPS over WebSocket, registers (first run) or
authenticates (subsequent runs) using a locally cached credential, then
waits for structured commands and executes them.

Usage:
    python3 harness_client.py --server wss://your-vps.example.com --token <enrollment_token>

After first successful registration, the credential is cached in
~/.agent-harness/credential.json and --token is no longer needed.
"""
import argparse
import json
import os
import platform
import socket
import ssl
import subprocess
import sys
import time
import urllib.request

try:
    import websocket  # websocket-client package
except ImportError:
    sys.exit("Missing dependency: pip install websocket-client")

CRED_DIR = os.path.join(os.path.expanduser("~"), ".agent-harness")
CRED_PATH = os.path.join(CRED_DIR, "credential.json")

RECONNECT_BASE_S = 2
RECONNECT_MAX_S = 60
COMMAND_OUTPUT_CAP = 50_000


def http_base_from_ws(server: str) -> str:
    return server.replace("wss://", "https://").replace("ws://", "http://")


def load_credential():
    if os.path.exists(CRED_PATH):
        with open(CRED_PATH) as f:
            return json.load(f)
    return None


def save_credential(device_id: str, credential: str):
    os.makedirs(CRED_DIR, exist_ok=True)
    with open(CRED_PATH, "w") as f:
        json.dump({"device_id": device_id, "credential": credential}, f)
    try:
        os.chmod(CRED_PATH, 0o600)
    except OSError:
        pass


def delete_credential():
    if os.path.exists(CRED_PATH):
        os.remove(CRED_PATH)


def _insecure_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def register(server: str, token: str, insecure: bool = False):
    url = http_base_from_ws(server) + "/api/register"
    body = json.dumps(
        {"token": token, "name": socket.gethostname(), "os": platform.system()}
    ).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    kwargs = {"timeout": 15}
    if insecure and url.startswith("https://"):
        kwargs["context"] = _insecure_ssl_context()
    with urllib.request.urlopen(req, **kwargs) as resp:
        data = json.loads(resp.read())
    if "error" in data:
        sys.exit(f"Registration failed: {data['error']}")
    save_credential(data["device_id"], data["credential"])
    return data["device_id"], data["credential"]


def run_command(command: str, timeout_s: int):
    start = time.time()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        elapsed_ms = int((time.time() - start) * 1000)
        return {
            "status": "completed",
            "exit_code": proc.returncode,
            "stdout": proc.stdout[:COMMAND_OUTPUT_CAP],
            "stderr": proc.stderr[:COMMAND_OUTPUT_CAP],
            "execution_ms": elapsed_ms,
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "exit_code": None, "stdout": "", "stderr": "command timed out"}
    except Exception as e:
        return {"status": "error", "exit_code": None, "stdout": "", "stderr": str(e)}


def run_forever(server: str, device_id: str, credential: str, insecure: bool = False):
    backoff = RECONNECT_BASE_S
    sslopt = {"cert_reqs": ssl.CERT_NONE, "check_hostname": False} if insecure else None
    while True:
        try:
            connect_kwargs = {"timeout": 15}
            if sslopt and server.startswith("wss://"):
                connect_kwargs["sslopt"] = sslopt
            ws = websocket.create_connection(server + "/ws/pc", **connect_kwargs)
            ws.send(json.dumps({"type": "auth", "device_id": device_id, "credential": credential}))
            reply = json.loads(ws.recv())
            if reply.get("type") != "auth_ok":
                # Retrying with the same credential would just fail the same way forever
                # (revoked device, server DB reset, etc. — not a transient network blip).
                # Clear the stale cache so the next run's error points at the real fix.
                delete_credential()
                sys.exit(
                    f"Auth rejected by server: {reply}. "
                    "Cached credential cleared — re-run with --token from a fresh dashboard enrollment."
                )

            print(f"Connected as {device_id}")
            backoff = RECONNECT_BASE_S
            ws.settimeout(30)

            while True:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    ws.send(json.dumps({"type": "ping"}))
                    continue
                if not raw:
                    break
                msg = json.loads(raw)
                if msg.get("type") == "command":
                    result = run_command(msg["command"], msg.get("timeout_s", 30))
                    result.update(
                        {
                            "type": "command_result",
                            "request_id": msg["request_id"],
                            "device_id": device_id,
                        }
                    )
                    ws.send(json.dumps(result))
                elif msg.get("type") == "pong":
                    pass
        except (websocket.WebSocketException, OSError) as e:
            print(f"Connection lost ({e}), retrying in {backoff}s", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_S)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True, help="wss://your-vps-ip")
    parser.add_argument("--token", help="enrollment token (first run only)")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS certificate verification (needed for Caddy's self-signed cert on a bare IP)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="discard any cached credential before starting (use with --token to force fresh registration)",
    )
    args = parser.parse_args()

    if args.reset:
        delete_credential()

    # A --token on the command line is an explicit instruction to (re-)register — always
    # honor it over whatever happens to be cached, rather than silently using a stale
    # credential and failing with an opaque auth_failed later. This is what --reset alone
    # used to require the user to notice and do manually.
    if args.token:
        device_id, credential = register(args.server, args.token, insecure=args.insecure)
    else:
        cred = load_credential()
        if not cred:
            sys.exit(
                "No cached credential found and no --token given — "
                "pass --token from the dashboard's enroll step"
            )
        device_id, credential = cred["device_id"], cred["credential"]

    run_forever(args.server, device_id, credential, insecure=args.insecure)


if __name__ == "__main__":
    main()
