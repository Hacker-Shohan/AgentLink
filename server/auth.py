import os
import base64
import secrets
import hashlib
import datetime as dt

from database import get_conn


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Enrollment-token lifetime. Default comes from HARNESS_ENROLL_TTL_MINUTES;
# callers may also pass an explicit ttl_minutes (clamped to MIN..MAX).
ENROLLMENT_TTL_MINUTES = _env_int("HARNESS_ENROLL_TTL_MINUTES", 30)
ENROLLMENT_TTL_MIN_MINUTES = _env_int("HARNESS_ENROLL_TTL_MIN_MINUTES", 1)
ENROLLMENT_TTL_MAX_MINUTES = _env_int("HARNESS_ENROLL_TTL_MAX_MINUTES", 1440)  # 24h cap


def resolve_ttl_minutes(ttl_minutes=None) -> int:
    """Effective TTL in minutes. None/empty -> configured default.
    An explicit value must be an integer within [MIN, MAX] or ValueError is raised."""
    if ttl_minutes is None or ttl_minutes == "":
        return ENROLLMENT_TTL_MINUTES
    try:
        val = int(ttl_minutes)
    except (TypeError, ValueError):
        raise ValueError("ttl_minutes must be an integer")
    if val < ENROLLMENT_TTL_MIN_MINUTES or val > ENROLLMENT_TTL_MAX_MINUTES:
        raise ValueError(
            f"ttl_minutes must be between {ENROLLMENT_TTL_MIN_MINUTES} and {ENROLLMENT_TTL_MAX_MINUTES}"
        )
    return val


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_device_id() -> str:
    return "DEV-" + secrets.token_hex(3).upper()


def create_enrollment_token(device_name: str = "", ttl_minutes=None) -> tuple[str, str]:
    """Returns (device_id, plaintext_token). Only the hash is stored.
    ttl_minutes overrides the configured default for this token only."""
    ttl = resolve_ttl_minutes(ttl_minutes)
    device_id = generate_device_id()
    token = secrets.token_urlsafe(24)
    now = dt.datetime.utcnow()
    expires = now + dt.timedelta(minutes=ttl)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO enrollment_tokens (token_hash, device_id, created_at, expires_at, used) "
            "VALUES (?, ?, ?, ?, 0)",
            (_hash(token), device_id, now.isoformat(), expires.isoformat()),
        )
    return device_id, token


def consume_enrollment_token(token: str):
    """Validates + marks a single-use enrollment token used. Returns device_id or None."""
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM enrollment_tokens WHERE token_hash = ? AND used = 0 AND expires_at > ?",
            (_hash(token), now),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE enrollment_tokens SET used = 1 WHERE token_hash = ?", (_hash(token),)
        )
        return row["device_id"]


def issue_device_credential(device_id: str, name: str, os_name: str) -> str:
    """Creates the device's persistent credential, replacing the enrollment token."""
    cred = secrets.token_urlsafe(32)
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO devices (device_id, name, os, cred_hash, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'OFFLINE') "
            "ON CONFLICT(device_id) DO UPDATE SET cred_hash=excluded.cred_hash, os=excluded.os",
            (device_id, name, os_name, _hash(cred), now),
        )
    return cred


def verify_device_credential(device_id: str, cred: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT cred_hash, revoked FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if not row or row["revoked"]:
            return False
        return row["cred_hash"] == _hash(cred)


def revoke_device(device_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE devices SET revoked = 1, status = 'OFFLINE' WHERE device_id = ?", (device_id,))


def delete_device(device_id: str) -> int:
    """Permanently removes a device row and its enrollment tokens.
    Returns the number of device rows deleted (0 if not found).
    Command history is intentionally preserved."""
    with get_conn() as conn:
        conn.execute("DELETE FROM enrollment_tokens WHERE device_id = ?", (device_id,))
        cur = conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
        return cur.rowcount


def reissue_enrollment_token(device_id: str, ttl_minutes=None) -> str:
    """New enrollment token for a device_id that already exists (re-run of setup,
    or the client's cached credential went stale). Does not touch devices table;
    /api/register will overwrite the existing row's cred_hash on redemption.
    ttl_minutes overrides the configured default for this token only."""
    ttl = resolve_ttl_minutes(ttl_minutes)
    token = secrets.token_urlsafe(24)
    now = dt.datetime.utcnow()
    expires = now + dt.timedelta(minutes=ttl)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO enrollment_tokens (token_hash, device_id, created_at, expires_at, used) "
            "VALUES (?, ?, ?, ?, 0)",
            (_hash(token), device_id, now.isoformat(), expires.isoformat()),
        )
    return token


def build_windows_bootstrap_command(base_url: str, token: str, device_id: str) -> str:
    """Returns `powershell -NoProfile -EncodedCommand <base64>`.

    -EncodedCommand sidesteps two separate problems with `powershell -c "..."`:
    1. PowerShell's own quote-stripping turns an unescaped `&` in the query
       string into the call operator mid-command -> parse error on paste.
    2. The query string's `?` and `&` would otherwise need per-shell escaping
       that differs between cmd.exe, PowerShell and Windows Terminal.
    Base64-encoding the whole script sidesteps both: nothing is reinterpreted
    by any shell between copy and execution.

    The script also shells out to curl.exe (bundled with Windows 10 1803+)
    instead of Invoke-WebRequest for both the bootstrap-script fetch and
    (inside bootstrap.ps1) the client download. iwr goes through .NET's own
    TLS stack, which on Windows PowerShell 5.1 can fail the handshake against
    a modern ECDSA self-signed cert with an opaque "underlying connection was
    closed" error that has nothing to do with certificate trust — no amount
    of ServerCertificateValidationCallback fixes it, because the handshake
    itself never completes. curl.exe uses its own TLS implementation and
    doesn't hit this.
    """
    url = f"{base_url}/bootstrap.ps1?token={token}&device_id={device_id}"
    script = (
        "$ErrorActionPreference='Stop'; "
        f"$u='{url}'; "
        "$o=\"$env:TEMP\\agent-harness-bootstrap.ps1\"; "
        "curl.exe -ksSL $u -o $o; "
        "if ($LASTEXITCODE -ne 0) { Write-Error 'bootstrap download failed'; exit 1 }; "
        # Windows' default execution policy blocks running ANY downloaded .ps1,
        # regardless of transport — unrelated to the TLS/curl.exe fix above.
        # Scope Process only changes it for this one process, not the machine,
        # and needs no admin rights.
        "Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force; "
        "& $o"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -EncodedCommand {encoded}"
