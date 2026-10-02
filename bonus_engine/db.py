"""SQLite 持久化层：schema 定义与连接管理。"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS distributors (
    id        INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES distributors(id),
    side      TEXT CHECK (side IN ('L', 'R'))
);
CREATE INDEX IF NOT EXISTS idx_distributors_parent ON distributors(parent_id);

-- 原始业绩流水（审计用；重算基于 week_state 聚合值）
CREATE TABLE IF NOT EXISTS volumes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    distributor_id INTEGER NOT NULL,
    week           INTEGER NOT NULL,
    points         INTEGER NOT NULL,
    order_ref      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_volumes_dist_week ON volumes(distributor_id, week);

-- 每分销商每周的已结算状态（稀疏：全零周不落行）
-- own/left_v/right_v 是“当前已知”的业绩聚合，迟到业绩会直接改这些列；
-- bonus/co_l/co_r 是上次重算得到的计算结果。
CREATE TABLE IF NOT EXISTS week_state (
    distributor_id INTEGER NOT NULL,
    week           INTEGER NOT NULL,
    own            INTEGER NOT NULL DEFAULT 0,
    left_v         INTEGER NOT NULL DEFAULT 0,
    right_v        INTEGER NOT NULL DEFAULT 0,
    bonus          INTEGER NOT NULL DEFAULT 0,
    co_l           INTEGER NOT NULL DEFAULT 0,
    co_r           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (distributor_id, week)
);

-- 待重算集合：分销商 -> 最早受影响周
CREATE TABLE IF NOT EXISTS dirty (
    distributor_id INTEGER PRIMARY KEY,
    earliest_week  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS debts (
    distributor_id INTEGER PRIMARY KEY,
    debt           INTEGER NOT NULL CHECK (debt >= 0)
);

CREATE TABLE IF NOT EXISTS batches (
    id   INTEGER PRIMARY KEY,
    week INTEGER NOT NULL
);

-- 已生成批次，任何字段不再变更
CREATE TABLE IF NOT EXISTS batch_lines (
    batch_id       INTEGER NOT NULL REFERENCES batches(id),
    distributor_id INTEGER NOT NULL,
    week_bonus     INTEGER NOT NULL,
    adjustment     INTEGER NOT NULL,
    paid           INTEGER NOT NULL CHECK (paid >= 0),
    debt           INTEGER NOT NULL CHECK (debt >= 0),
    PRIMARY KEY (batch_id, distributor_id)
);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开（必要时创建）状态数据库并确保 schema 存在。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('current_week', 1)"
    )
    conn.commit()
    return conn
