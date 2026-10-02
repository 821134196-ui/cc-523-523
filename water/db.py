"""SQLite 持久化。核心设计：账务只追加事件（ledger），不删除流水。"""
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sites (
    site_id   TEXT PRIMARY KEY,
    name      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operators (
    operator_id TEXT PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    site_id     TEXT NOT NULL REFERENCES sites(site_id),
    role        TEXT NOT NULL CHECK (role IN ('operator','supervisor'))
);

CREATE TABLE IF NOT EXISTS households (
    account_no   TEXT PRIMARY KEY,           -- 户号
    account_code TEXT NOT NULL,              -- 户名代号（脱敏，如 周**-07）
    village      TEXT NOT NULL,
    site_id      TEXT NOT NULL REFERENCES sites(site_id)
);

CREATE TABLE IF NOT EXISTS bills (
    bill_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    account_no   TEXT NOT NULL REFERENCES households(account_no),
    period       TEXT NOT NULL,              -- 账期 YYYY-MM
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    UNIQUE(account_no, period)
);

-- 追加式账务事件：收款为正、冲正/短款为负。任何更正都以新事件体现，原行保留。
CREATE TABLE IF NOT EXISTS ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id        TEXT NOT NULL UNIQUE,      -- 业务流水号
    site_id       TEXT NOT NULL REFERENCES sites(site_id),
    operator_id   TEXT NOT NULL REFERENCES operators(operator_id),
    event_type    TEXT NOT NULL CHECK (event_type IN ('PAYMENT','REVERSAL','SHORTAGE')),
    method        TEXT CHECK (method IS NULL OR method IN ('cash','electronic')),
    channel       TEXT,                      -- alipay / wechat / bankcash
    amount_cents  INTEGER NOT NULL,          -- 有符号：收款正、冲正负、短款负
    account_no    TEXT REFERENCES households(account_no),
    bill_id       INTEGER REFERENCES bills(bill_id),
    period        TEXT,
    pay_ref       TEXT,                      -- 支付/退款渠道流水号
    pay_status    TEXT NOT NULL DEFAULT 'SUCCESS'
                  CHECK (pay_status IN ('PENDING','SUCCESS','FAILED')),
    water_status  TEXT NOT NULL DEFAULT 'PENDING'
                  CHECK (water_status IN ('PENDING','CONFIRMED','REJECTED')),
    related_txn   TEXT REFERENCES ledger(txn_id),  -- 冲正必须引用原交易
    shift_id      INTEGER REFERENCES shifts(shift_id),
    reason_code   TEXT,                      -- wrong_account / duplicate / cross_period / gateway / other / shortage
    memo          TEXT,
    receipt_no    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    settled_at    TEXT
);

-- 渠道流水号唯一：同一支付回执重复送达只能落到同一事件（幂等）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_payref
    ON ledger(pay_ref) WHERE pay_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ledger_bill ON ledger(bill_id);
CREATE INDEX IF NOT EXISTS idx_ledger_site_time ON ledger(site_id, created_at);

CREATE TABLE IF NOT EXISTS receipts (
    receipt_no       TEXT PRIMARY KEY,
    txn_id           TEXT NOT NULL REFERENCES ledger(txn_id),
    kind             TEXT NOT NULL CHECK (kind IN ('NORMAL','REDLINE','VOID')),
    file_path        TEXT NOT NULL,
    linked_receipt_no TEXT REFERENCES receipts(receipt_no),  -- 红字单关联被作废原单
    voided           INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS approval_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reversal_txn_id TEXT UNIQUE REFERENCES ledger(txn_id),  -- 批准后生成的冲正事件
    original_txn_id TEXT NOT NULL REFERENCES ledger(txn_id),
    site_id         TEXT NOT NULL REFERENCES sites(site_id),
    requested_by    TEXT NOT NULL REFERENCES operators(operator_id),
    reason_code     TEXT NOT NULL,
    reason_detail   TEXT,
    status          TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING','APPROVED','REJECTED')),
    reviewer_id     TEXT REFERENCES operators(operator_id),
    review_comment  TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    reviewed_at     TEXT
    -- 是否允许重复申请由服务层判定（PENDING/APPROVED 互斥，驳回后可再次申请）
);

CREATE TABLE IF NOT EXISTS shifts (
    shift_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id             TEXT NOT NULL REFERENCES sites(site_id),
    operator_id         TEXT NOT NULL REFERENCES operators(operator_id),
    opened_at           TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    closed_at           TEXT,
    opening_float_cents INTEGER NOT NULL DEFAULT 0,
    expected_cash_cents INTEGER,
    actual_cash_cents   INTEGER,
    shortage_cents      INTEGER,
    shortage_txn_id     TEXT REFERENCES ledger(txn_id),
    note                TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
