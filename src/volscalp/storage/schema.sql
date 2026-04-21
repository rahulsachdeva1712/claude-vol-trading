-- volscalp persistence schema. Applied idempotently on startup.
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date    TEXT NOT NULL,          -- YYYY-MM-DD
    index_name      TEXT,                   -- NIFTY | BANKNIFTY | ALL
    mode            TEXT NOT NULL,          -- paper | live
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    total_cycles    INTEGER DEFAULT 0,
    realized_pnl    REAL DEFAULT 0,
    peak_pnl        REAL DEFAULT 0,
    trough_pnl      REAL DEFAULT 0,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(session_date);

CREATE TABLE IF NOT EXISTS cycles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    cycle_no        INTEGER NOT NULL,
    underlying      TEXT NOT NULL,
    atm_at_start    INTEGER,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    exit_reason     TEXT,
    peak_mtm        REAL DEFAULT 0,
    trough_mtm      REAL DEFAULT 0,
    cycle_pnl       REAL DEFAULT 0,
    lock_activated  INTEGER DEFAULT 0,
    lock_floor      REAL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cycles_session ON cycles(session_id);

CREATE TABLE IF NOT EXISTS legs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id         INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
    slot             INTEGER NOT NULL,
    kind             TEXT NOT NULL,          -- BASE | LAZY
    option_type      TEXT NOT NULL,          -- CE | PE
    underlying       TEXT NOT NULL,
    strike           INTEGER NOT NULL,
    expiry           TEXT NOT NULL,
    security_id      INTEGER,
    trading_symbol   TEXT,
    lot_size         INTEGER,
    lots             INTEGER,
    quantity         INTEGER,
    status           TEXT NOT NULL,
    entry_ts         TEXT,
    entry_price      REAL,
    sl_price         REAL,
    exit_ts          TEXT,
    exit_price       REAL,
    exit_reason      TEXT,
    pnl              REAL DEFAULT 0,
    entry_order_id   TEXT,
    exit_order_id    TEXT
);

CREATE INDEX IF NOT EXISTS idx_legs_cycle ON legs(cycle_id);

CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id   TEXT UNIQUE NOT NULL,
    broker_order_id   TEXT,
    session_id        INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    cycle_id          INTEGER REFERENCES cycles(id)   ON DELETE SET NULL,
    leg_id            INTEGER REFERENCES legs(id)     ON DELETE SET NULL,
    security_id       INTEGER NOT NULL,
    trading_symbol    TEXT,
    side              TEXT NOT NULL,
    quantity          INTEGER NOT NULL,
    order_type        TEXT,
    product_type      TEXT,
    status            TEXT NOT NULL,
    filled_quantity   INTEGER DEFAULT 0,
    avg_fill_price    REAL DEFAULT 0,
    message           TEXT,
    placed_at         TEXT NOT NULL,
    updated_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(session_id);
CREATE INDEX IF NOT EXISTS idx_orders_broker  ON orders(broker_order_id);

CREATE TABLE IF NOT EXISTS bar_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    cycle_id         INTEGER REFERENCES cycles(id) ON DELETE SET NULL,
    minute_epoch     INTEGER NOT NULL,
    underlying       TEXT,
    spot             REAL,
    atm              INTEGER,
    payload_json     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bar_snaps_session ON bar_snapshots(session_id);

CREATE TABLE IF NOT EXISTS decisions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    cycle_id         INTEGER REFERENCES cycles(id)   ON DELETE SET NULL,
    ts               TEXT NOT NULL,
    kind             TEXT NOT NULL,      -- entry_eval | exit_eval | mtm | ...
    inputs_json      TEXT,
    outcome          TEXT
);
