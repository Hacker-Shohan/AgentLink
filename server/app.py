import functools
import json
import os
import secrets
import sys

from flask import Flask, jsonify, render_template, request, Response
from flask_sock import Sock
from werkzeug.middleware.proxy_fix import ProxyFix

sys.path.insert(0, os.path.dirname(__file__))

from database import init_db
import auth
import devices
import commands

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "..", "web", "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "..", "web", "static"),
)
sock = Sock(app)

# Trust exactly one reverse-proxy hop (Caddy) for X-Forwarded-Proto/Host/For.
# Without this, request.host_url reports Flask's own view (http://127.0.0.1:8787)
# instead of what the client actually hit (https://<vps-ip>), which broke every
# generated bootstrap command. If you ever stack a second proxy in front of Caddy,
# bump x_proto/x_host to 2 — leaving it at 1 with two real hops lets the outer
# proxy spoof these headers.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

init_db()


# ---------- Dashboard auth ----------
# /ws/pc (devices connect here) and the bootstrap/client routes stay open —
# a PC has no password to send. Everything that can control a device
# (the dashboard UI and every /api/* route) requires this.
#
# Set these before running in anything but a quick local test:
#   export HARNESS_DASHBOARD_USER=admin
#   export HARNESS_DASHBOARD_PASS=<something long and random>
DASHBOARD_USER = os.environ.get("HARNESS_DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.environ.get("HARNESS_DASHBOARD_PASS")
if not DASHBOARD_PASS:
    DASHBOARD_PASS = secrets.token_urlsafe(16)
    print(
        f"\n[!] HARNESS_DASHBOARD_PASS not set — generated one for this run:\n"
        f"    user: {DASHBOARD_USER}\n"
        f"    pass: {DASHBOARD_PASS}\n"
        f"    Set HARNESS_DASHBOARD_USER/HARNESS_DASHBOARD_PASS env vars to persist this.\n",
        file=sys.stderr,
    )


def require_dashboard_auth(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not auth or not (
            secrets.compare_digest(auth.username or "", DASHBOARD_USER)
            and secrets.compare_digest(auth.password or "", DASHBOARD_PASS)
        ):
            return Response(
                "Authentication required", 401, {"WWW-Authenticate": 'Basic realm="agent-hub"'}
            )
        return view(*args, **kwargs)

    return wrapped


# ---------- Dashboard ----------

@app.route("/")
@require_dashboard_auth
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/devices")
@require_dashboard_auth
def api_devices():
    rows = devices.list_devices()
    live = devices.connected_clients
    for r in rows:
        r["connected"] = r["device_id"] in live
    return jsonify(rows)


@app.route("/api/history")
@require_dashboard_auth
def api_history():
    return jsonify(commands.recent_history())


# ---------- Enrollment ----------

@app.route("/api/enroll", methods=["POST"])
@require_dashboard_auth
def api_enroll():
    name = (request.json or {}).get("name", "") if request.is_json else ""
    device_id, token = auth.create_enrollment_token(name)
    base_url = request.host_url.rstrip("/")

    ps_command = auth.build_windows_bootstrap_command(base_url, token, device_id)
    sh_command = (
        f'curl -fsSLk "{base_url}/bootstrap.sh?token={token}&device_id={device_id}" | bash'
    )
    return jsonify(
        {
            "device_id": device_id,
            "token": token,
            "expires_in_minutes": auth.ENROLLMENT_TTL_MINUTES,
            "windows_command": ps_command,
            "linux_command": sh_command,
        }
    )


@app.route("/api/register", methods=["POST"])
def api_register():
    """Called by the PC client with its (single-use) enrollment token — not dashboard-authed,
    the token itself is the proof. consume_enrollment_token() rejects anything invalid/expired/reused."""
    data = request.json or {}
    token = data.get("token")
    name = data.get("name", "")
    os_name = data.get("os", "")

    device_id = auth.consume_enrollment_token(token)
    if not device_id:
        return jsonify({"error": "invalid_or_expired_token"}), 400

    cred = auth.issue_device_credential(device_id, name, os_name)
    return jsonify({"device_id": device_id, "credential": cred})


@app.route("/api/devices/<device_id>/reissue-token", methods=["POST"])
@require_dashboard_auth
def api_reissue_token(device_id):
    """A fresh enrollment token for a device_id that already exists — for re-running
    setup (stale/expired/already-used token) without creating a duplicate device row."""
    token = auth.reissue_enrollment_token(device_id)
    base_url = request.host_url.rstrip("/")
    return jsonify(
        {
            "device_id": device_id,
            "token": token,
            "expires_in_minutes": auth.ENROLLMENT_TTL_MINUTES,
            "windows_command": auth.build_windows_bootstrap_command(base_url, token, device_id),
            "linux_command": f'curl -fsSLk "{base_url}/bootstrap.sh?token={token}&device_id={device_id}" | bash',
        }
    )


@app.route("/api/devices/<device_id>/revoke", methods=["POST"])
@require_dashboard_auth
def api_revoke(device_id):
    auth.revoke_device(device_id)
    conn_entry = devices.get_connection(device_id)
    if conn_entry:
        try:
            conn_entry["ws"].close()
        except Exception:
            pass
    return jsonify({"ok": True})


# ---------- Commands ----------

@app.route("/api/devices/<device_id>/command", methods=["POST"])
@require_dashboard_auth
def api_command(device_id):
    data = request.json or {}
    command = data.get("command")
    timeout_s = int(data.get("timeout_s", commands.DEFAULT_TIMEOUT_S))
    if not command:
        return jsonify({"error": "missing_command"}), 400
    result = commands.send_command(device_id, command, timeout_s)
    return jsonify(result)


# ---------- Bootstrap / client delivery ----------

CLIENT_DIR = os.path.join(os.path.dirname(__file__), "..", "client")


@app.route("/bootstrap.ps1")
def bootstrap_ps1():
    token = request.args.get("token", "")
    device_id = request.args.get("device_id", "")
    path = os.path.join(CLIENT_DIR, "windows", "bootstrap.ps1")
    with open(path) as f:
        text = f.read()
    text = (
        text.replace("{{SERVER_BASE}}", request.host_url.rstrip("/"))
        .replace("{{TOKEN}}", token)
        .replace("{{DEVICE_ID}}", device_id)
    )
    return text, 200, {"Content-Type": "text/plain"}


@app.route("/bootstrap.sh")
def bootstrap_sh():
    token = request.args.get("token", "")
    path = os.path.join(CLIENT_DIR, "linux", "bootstrap.sh")
    with open(path) as f:
        text = f.read()
    text = text.replace("{{SERVER_BASE}}", request.host_url.rstrip("/")).replace("{{TOKEN}}", token)
    return text, 200, {"Content-Type": "text/plain"}


@app.route("/client/harness_client.py")
def client_py():
    path = os.path.join(CLIENT_DIR, "harness_client.py")
    with open(path) as f:
        text = f.read()
    return text, 200, {"Content-Type": "text/plain"}


# ---------- WebSocket (PC <-> VPS) ----------

@sock.route("/ws/pc")
def ws_pc(ws):
    device_id = None
    try:
        # First message from the client must be an auth frame.
        raw = ws.receive(timeout=10)
        if raw is None:
            return
        msg = json.loads(raw)
        if msg.get("type") != "auth":
            ws.send(json.dumps({"type": "error", "message": "expected_auth"}))
            return

        device_id = msg.get("device_id")
        cred = msg.get("credential")
        if not device_id or not cred or not auth.verify_device_credential(device_id, cred):
            ws.send(json.dumps({"type": "error", "message": "auth_failed"}))
            return

        devices.register_connection(device_id, ws)
        ws.send(json.dumps({"type": "auth_ok", "device_id": device_id}))

        while True:
            raw = ws.receive()
            if raw is None:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")
            if mtype == "command_result":
                commands.record_result(msg)
            elif mtype == "ping":
                devices.touch_last_seen(device_id)
                ws.send(json.dumps({"type": "pong"}))
    except Exception:
        pass
    finally:
        if device_id:
            devices.unregister_connection(device_id)


if __name__ == "__main__":
    # With nginx in front (see deploy/nginx.conf), nginx is the only thing
    # with a public IP — Flask only needs to be reachable from nginx on the
    # same box, so bind to localhost. Only run with HARNESS_BIND_PUBLIC=1
    # if you're intentionally skipping the reverse proxy (not recommended:
    # no TLS, Basic Auth password goes over the wire in the clear).
    debug = os.environ.get("HARNESS_DEBUG") == "1"
    port = int(os.environ.get("HARNESS_PORT", "8787"))
    host = "0.0.0.0" if os.environ.get("HARNESS_BIND_PUBLIC") == "1" else "127.0.0.1"
    app.run(host=host, port=port, debug=debug)
