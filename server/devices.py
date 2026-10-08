import datetime as dt
import threading

from database import get_conn

# device_id -> {"ws": <socket>, "lock": threading.Lock()}
connected_clients: dict[str, dict] = {}
_registry_lock = threading.Lock()


def register_connection(device_id: str, ws):
    with _registry_lock:
        connected_clients[device_id] = {"ws": ws, "lock": threading.Lock()}
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE devices SET status = 'ONLINE', last_seen = ? WHERE device_id = ?",
            (now, device_id),
        )


def unregister_connection(device_id: str):
    with _registry_lock:
        connected_clients.pop(device_id, None)
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE devices SET status = 'OFFLINE', last_seen = ? WHERE device_id = ?",
            (now, device_id),
        )


def touch_last_seen(device_id: str):
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE devices SET last_seen = ? WHERE device_id = ?", (now, device_id))


def get_connection(device_id: str):
    with _registry_lock:
        return connected_clients.get(device_id)


def list_devices():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT device_id, name, os, status, last_seen, revoked FROM devices ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
