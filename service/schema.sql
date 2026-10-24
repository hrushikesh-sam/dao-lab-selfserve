-- service/schema.sql — daolab BASELINE schema (v0; sqlite, WAL set at connect time)
-- Frozen: do not edit existing tables here. Schema changes after this baseline are
-- numbered migrations in service/migrations.py, applied by `daolab init` (idempotent).

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL UNIQUE,
    role        TEXT    NOT NULL DEFAULT 'user'
                CHECK (role IN ('admin', 'user')),
    enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    token_hash  TEXT    NOT NULL UNIQUE,          -- SHA-256 hex of the bearer token
    ssh_pubkey  TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,                 -- UTC ISO-8601 YYYY-MM-DDTHH:MM:SSZ
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS pools (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,          -- e.g. 'lab-1'
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS setups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE,         -- e.g. 'setup-01'
    pool_id      INTEGER NOT NULL REFERENCES pools(id),
    capabilities TEXT    NOT NULL DEFAULT '[]',   -- JSON array; computed union, sorted
    links        TEXT    NOT NULL DEFAULT '[]',   -- JSON array of {a,b,capability}
    diagram      TEXT    NOT NULL DEFAULT '',     -- mermaid source, rendered verbatim
    yaml_path    TEXT    NOT NULL,                -- e.g. 'setups/setup-01.yml'
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS resources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_id     INTEGER NOT NULL REFERENCES setups(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,                -- member name, e.g. 'dpu-01'
    kind         TEXT    NOT NULL
                 CHECK (kind IN ('dpu', 'gpu', 'nic', 'compute')),
    capabilities TEXT    NOT NULL DEFAULT '[]',   -- JSON array
    host         TEXT    NOT NULL,                -- XCP-ng host, e.g. 'dpu-01-xcp'
    hw_class     TEXT,                            -- current DPU personality; NULL for non-DPU
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    UNIQUE (setup_id, name)
);

CREATE TABLE IF NOT EXISTS usecases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,          -- e.g. 'tls-proxy-nginx', 'diy'
    description TEXT    NOT NULL DEFAULT '',      -- "what it is" column of the README table
    doc_url     TEXT,                             -- DAO documentation page for this application
    doc_summary TEXT,                             -- verbatim intro text from the DAO docs
    variant     TEXT,                             -- e.g. 'separate-backend'; NULL if planned/diy
    requires    TEXT    NOT NULL DEFAULT '[]',    -- JSON array of capability strings
    hw_class    TEXT,                             -- e.g. 'sdp-pf1-vf8'; NULL = no EBF requirement
    roles       TEXT    NOT NULL DEFAULT '{}',    -- JSON object, role name -> role spec
    verify      TEXT,                             -- JSON object {from,cmd,expect}; NULL if planned/diy
    yaml_path   TEXT,                             -- NULL = planned (no YAML yet); 'builtin' for diy
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL REFERENCES users(id),
    usecase_id         INTEGER NOT NULL REFERENCES usecases(id),
    setup_id           INTEGER NOT NULL REFERENCES setups(id),
    state              TEXT    NOT NULL DEFAULT 'queued'
                       CHECK (state IN ('queued', 'provisioning', 'ready', 'active',
                                        'tearing_down', 'released', 'failed')),
    requested_start_at TEXT    NOT NULL,          -- what the user asked for
    start_at           TEXT    NOT NULL,          -- what the scheduler granted (slot-shift)
    end_at             TEXT    NOT NULL,
    diy                INTEGER NOT NULL DEFAULT 0 CHECK (diy IN (0, 1)),
    diy_templates      TEXT,                      -- JSON object host -> template; NULL unless diy
    retries            INTEGER NOT NULL DEFAULT 0,
    failure_reason     TEXT,                      -- set only in state 'failed'
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS vms (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
    role           TEXT    NOT NULL,              -- role name from usecase roles; host name for diy
    host           TEXT    NOT NULL,              -- XCP-ng host (or DPU node name for on: dpu roles)
    template       TEXT,                          -- NULL for on: dpu roles (no VM template)
    name           TEXT,                          -- XCP-ng VM name: 'daolab-r<id>-<role>'; NULL for dpu roles
    ip             TEXT,                          -- posted by POST /internal/vm-ip; NULL until then
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    UNIQUE (reservation_id, role)
);

CREATE INDEX IF NOT EXISTS idx_resources_setup       ON resources(setup_id);
CREATE INDEX IF NOT EXISTS idx_reservations_setup    ON reservations(setup_id, start_at, end_at);
CREATE INDEX IF NOT EXISTS idx_reservations_state    ON reservations(state);
CREATE INDEX IF NOT EXISTS idx_reservations_user     ON reservations(user_id);
CREATE INDEX IF NOT EXISTS idx_vms_reservation       ON vms(reservation_id);
