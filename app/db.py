"""SQLite 连接与事件流水 schema。

账务只追加（append-only）：任何更正都以冲正事件实现，不删除、不更新流水。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;

-- 服务点（村镇便民点）
CREATE TABLE IF NOT EXISTS sites (
    code        TEXT PRIMARY KEY,          -- 站点编号
    name        TEXT NOT NULL
);

-- 操作员 / 主管
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('operator','supervisor')),
    site_code   TEXT REFERENCES sites(code),  -- 主管可跨站点，为 NULL
    CHECK (role = 'supervisor' OR site_code IS NOT NULL)
);

-- 居民户头
CREATE TABLE IF NOT EXISTS households (
    id          TEXT PRIMARY KEY,
    account_no  TEXT NOT NULL UNIQUE,       -- 户号
    name_code   TEXT NOT NULL,              -- 户名代号（不存真实姓名）
    site_code   TEXT NOT NULL REFERENCES sites(code)  -- 归属服务点
);

-- 账单（按户 + 账期唯一）
CREATE TABLE IF NOT EXISTS bills (
    id          TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    period      TEXT NOT NULL,              -- 账期 YYYY-MM
    amount_due  INTEGER NOT NULL,           -- 应缴金额（分）
    UNIQUE (household_id, period)
);

-- 账务事件流水（只追加）
-- 金额方向：
--   PAYMENT        缴款金额（正）；pay_status=PENDING 表示渠道延迟到账
--   PAY_CONFIRM    零金额事件：延迟支付到账确认（审计轨迹，不改动原事件）
--   WATER_CONFIRM  零金额事件：水务销账回执（延迟到达时单独补登）
--   REVERSAL       amount 为负 = 冲正原缴款；reverses_id 引用原事件
--   SHORTAGE       amount 为负 = 现金短款（操作员赔付责任）
--   SHORTAGE_REPAY amount 为正 = 短款补缴
CREATE TABLE IF NOT EXISTS ledger_events (
    id              TEXT PRIMARY KEY,
    seq             INTEGER NOT NULL,
    bill_id         TEXT REFERENCES bills(id),      -- 短款事件可为空
    site_code       TEXT NOT NULL REFERENCES sites(code),
    operator_id     TEXT NOT NULL REFERENCES users(id),
    kind            TEXT NOT NULL CHECK (kind IN
                    ('PAYMENT','PAY_CONFIRM','WATER_CONFIRM',
                     'REVERSAL','SHORTAGE','SHORTAGE_REPAY')),
    amount          INTEGER NOT NULL,              -- 带符号，单位分
    channel         TEXT CHECK (channel IN ('CASH','ELECTRONIC')),
    pay_ref         TEXT,                          -- 支付渠道流水号
    pay_status      TEXT,                          -- PENDING / SUCCESS
    receipt_no      TEXT UNIQUE,                   -- 收据号（作废也保留）
    receipt_status  TEXT CHECK (receipt_status IN ('VALID','VOID')),
    reverses_id     TEXT REFERENCES ledger_events(id),
    confirms_id     TEXT REFERENCES ledger_events(id),  -- 确认事件对应的缴款
    approved_by     TEXT REFERENCES users(id),     -- 冲正主管批准人
    reason          TEXT,
    water_ref       TEXT,                          -- 水务回执号
    created_at      TEXT NOT NULL,
    UNIQUE (bill_id, pay_ref)                      -- 同一账单支付回执幂等
);

CREATE TABLE IF NOT EXISTS reversal_requests (
    id              TEXT PRIMARY KEY,
    original_id     TEXT NOT NULL REFERENCES ledger_events(id),
    requested_by    TEXT NOT NULL REFERENCES users(id),
    approver_id     TEXT REFERENCES users(id),
    status          TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED')),
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    decided_at      TEXT
);

CREATE TABLE IF NOT EXISTS shifts (
    id              TEXT PRIMARY KEY,
    site_code       TEXT NOT NULL REFERENCES sites(code),
    operator_id     TEXT NOT NULL REFERENCES users(id),
    opened_at       TEXT NOT NULL,
    closed_at       TEXT,
    cash_counted    INTEGER,                       -- 交班实点现金（分）
    note            TEXT
);

CREATE TABLE IF NOT EXISTS seq_counter (
    name    TEXT PRIMARY KEY,
    value   INTEGER NOT NULL
);
"""


def connect(db_path: str | Path, check_same_thread: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path), timeout=30, isolation_level=None,
        check_same_thread=check_same_thread,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def next_id(prefix: str) -> str:
    """短随机 id，无需时间戳依赖（测试中亦可读）。"""
    import secrets

    return f"{prefix}_{secrets.token_hex(6)}"


def next_seq(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO seq_counter(name, value) VALUES('event', 1) "
        "ON CONFLICT(name) DO UPDATE SET value = value + 1"
    )
    row = conn.execute("SELECT value FROM seq_counter WHERE name='event'").fetchone()
    return int(row["value"])
