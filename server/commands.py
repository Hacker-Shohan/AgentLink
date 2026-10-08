import datetime as dt
import json
import secrets
import threading

from database import get_conn
from devices import get_connection

DEFAULT_TIMEOUT_S = 30
MAX_OUTPUT_CHARS = 50_000

# request_id -> threading.Event, used to block the HTTP caller until a result arrives
_pending_events: dict[str, threading.Event] = {}
_pending_results: dict[str, dict] = {}
_lock = threading.Lock()


def send_command(device_id: str, command: str, timeout_s: int = DEFAULT_TIMEOUT_S):
    conn_entry = get_connection(device_id)
    if not conn_entry:
        return {"error": "device_offline"}

    request_id = "cmd_" + secrets.token_hex(4)
    now = dt.datetime.utcnow().isoformat()

    with get_conn() as db:
        db.execute(
            "INSERT INTO commands (request_id, device_id, command, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (request_id, device_id, command, now),
        )

    event = threading.Event()
    with _lock:
        _pending_events[request_id] = event

    payload = {
        "type": "command",
        "request_id": request_id,
        "device_id": device_id,
        "command": command,
        "timeout_s": timeout_s,
    }
    try:
        with conn_entry["lock"]:
            conn_entry["ws"].send(json.dumps(payload))
    except Exception as e:
        with _lock:
            _pending_events.pop(request_id, None)
        return {"error": f"send_failed: {e}"}

    completed = event.wait(timeout_s + 5)
    with _lock:
        result = _pending_results.pop(request_id, None)
        _pending_events.pop(request_id, None)

    if not completed or result is None:
        _mark_timeout(request_id)
        return {"request_id": request_id, "status": "timeout"}

    return result


def record_result(message: dict):
    """Called from the websocket handler when a PC sends back a command_result."""
    request_id = message.get("request_id")
    device_id = message.get("device_id")
    status = message.get("status", "completed")
    exit_code = message.get("exit_code")
    stdout = (message.get("stdout") or "")[:MAX_OUTPUT_CHARS]
    stderr = (message.get("stderr") or "")[:MAX_OUTPUT_CHARS]
    now = dt.datetime.utcnow().isoformat()

    with get_conn() as db:
        db.execute(
            "UPDATE commands SET status=?, exit_code=?, stdout=?, stderr=?, completed_at=? "
            "WHERE request_id=?",
            (status, exit_code, stdout, stderr, now, request_id),
        )

    result = {
        "request_id": request_id,
        "device_id": device_id,
        "status": status,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }

    with _lock:
        event = _pending_events.get(request_id)
        if event:
            _pending_results[request_id] = result
            event.set()

    return result


def _mark_timeout(request_id: str):
    with get_conn() as db:
        db.execute(
            "UPDATE commands SET status='timeout' WHERE request_id=? AND status='pending'",
            (request_id,),
        )


def recent_history(limit: int = 50):
    with get_conn() as db:
        rows = db.execute(
            "SELECT request_id, device_id, command, status, exit_code, created_at, completed_at "
            "FROM commands ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
