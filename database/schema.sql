CREATE TABLE IF NOT EXISTS enrollment_tokens (
    token_hash   TEXT PRIMARY KEY,
    device_id    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    used         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS devices (
    device_id      TEXT PRIMARY KEY,
    name           TEXT,
    os             TEXT,
    cred_hash      TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    last_seen      TEXT,
    status         TEXT NOT NULL DEFAULT 'OFFLINE',
    revoked        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS commands (
    request_id     TEXT PRIMARY KEY,
    device_id      TEXT NOT NULL,
    command        TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    exit_code      INTEGER,
    stdout         TEXT,
    stderr         TEXT,
    created_at     TEXT NOT NULL,
    completed_at   TEXT,
    execution_ms   INTEGER
);
