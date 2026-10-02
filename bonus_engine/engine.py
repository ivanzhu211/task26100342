"""双轨制奖金周结算引擎（SQLite 持久化，支持迟到业绩回溯重述）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

ACTIVE_THRESHOLD = 100        # 本周本人业绩不低于 100 点才算活跃
BONUS_PER_POINT = 10          # 每配对 1 点得 10 分
WEEKLY_BONUS_CAP = 200_000    # 单周奖金封顶（分）
CARRY_CAP = 20_000            # 每侧最多结转到下一周的点数


@dataclass(frozen=True)
class Line:
    """批次中某个分销商的一行。"""

    dist_id: int
    week_bonus: int   # 本周奖金（按当前已知业绩重述后的值）
    adjustment: int   # 对旧周的调整（迟到业绩/退货引起的重述差额，可负）
    paid: int         # 本次实发（>= 0）
    debt: int         # 结算后欠款（>= 0）


@dataclass(frozen=True)
class Batch:
    """一次 settle 生成的结算批次。"""

    batch_no: int
    week: int
    lines: tuple[Line, ...]

    def line_for(self, dist_id: int) -> Line:
        for line in self.lines:
            if line.dist_id == dist_id:
                return line
        raise KeyError(dist_id)


class Engine:
    """结算引擎。状态全部存于一个 SQLite 文件，关掉再打开能接着结算。"""

    def __init__(self, path: str):
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._create_schema()
        row = self._db.execute(
            "SELECT value FROM meta WHERE key='current_week'").fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO meta(key, value) VALUES('current_week', 1)")
            self._db.commit()
        # 测试/审计用：上一次 settle 重算实际触及的 (dist_id, week) 集合
        self.last_touched: set[tuple[int, int]] = set()

    def _create_schema(self) -> None:
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS distributors(
            id INTEGER PRIMARY KEY,
            parent_id INTEGER REFERENCES distributors(id),
            side TEXT CHECK(side IN ('L','R')));
        CREATE TABLE IF NOT EXISTS volumes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dist_id INTEGER NOT NULL REFERENCES distributors(id),
            week INTEGER NOT NULL,
            points INTEGER NOT NULL,
            order_ref TEXT NOT NULL);
        -- 每个分销商每周的业绩聚合：本人业绩 + 左/右子树当周业绩
        CREATE TABLE IF NOT EXISTS week_vol(
            dist_id INTEGER NOT NULL,
            week INTEGER NOT NULL,
            personal INTEGER NOT NULL DEFAULT 0,
            sub_l INTEGER NOT NULL DEFAULT 0,
            sub_r INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(dist_id, week));
        -- 每个分销商每个已结算周的计算结果：奖金与结转（结转为本周扣完后的转出）
        CREATE TABLE IF NOT EXISTS week_state(
            dist_id INTEGER NOT NULL,
            week INTEGER NOT NULL,
            bonus INTEGER NOT NULL,
            carry_l INTEGER NOT NULL,
            carry_r INTEGER NOT NULL,
            PRIMARY KEY(dist_id, week));
        CREATE TABLE IF NOT EXISTS accounts(
            dist_id INTEGER PRIMARY KEY,
            paid_total INTEGER NOT NULL DEFAULT 0,
            debt INTEGER NOT NULL DEFAULT 0,
            cum_bonus INTEGER NOT NULL DEFAULT 0);
        -- 上次 settle 之后新到的迟到业绩波及的人：所属人及其上线，含最早受影响周
        CREATE TABLE IF NOT EXISTS dirty(
            dist_id INTEGER PRIMARY KEY,
            min_week INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS batches(
            batch_no INTEGER PRIMARY KEY,
            week INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS batch_lines(
            batch_no INTEGER NOT NULL,
            dist_id INTEGER NOT NULL,
            week_bonus INTEGER NOT NULL,
            adjustment INTEGER NOT NULL,
            paid INTEGER NOT NULL,
            debt INTEGER NOT NULL,
            PRIMARY KEY(batch_no, dist_id));
        """)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def current_week(self) -> int:
        """当前未结周（从 1 开始）。"""
        return self._db.execute(
            "SELECT value FROM meta WHERE key='current_week'").fetchone()[0]

    def enroll(self, dist_id: int, parent_id: int | None = None,
               side: str | None = None) -> None:
        """把新分销商安置到父节点空着的一侧；根节点没有父。"""
        if parent_id is None:
            if side is not None:
                raise ValueError("根节点没有 side")
            self._db.execute(
                "INSERT INTO distributors(id, parent_id, side)"
                " VALUES(?, NULL, NULL)", (dist_id,))
        else:
            if side not in ("L", "R"):
                raise ValueError("side 必须是 'L' 或 'R'")
            parent = self._db.execute(
                "SELECT id FROM distributors WHERE id=?",
                (parent_id,)).fetchone()
            if parent is None:
                raise ValueError(f"父节点不存在: {parent_id}")
            occupied = self._db.execute(
                "SELECT id FROM distributors WHERE parent_id=? AND side=?",
                (parent_id, side)).fetchone()
            if occupied is not None:
                raise ValueError(f"父节点 {parent_id} 的 {side} 侧已被占用")
            self._db.execute(
                "INSERT INTO distributors(id, parent_id, side) VALUES(?,?,?)",
                (dist_id, parent_id, side))
        self._db.execute(
            "INSERT INTO accounts(dist_id) VALUES(?) ON CONFLICT DO NOTHING",
            (dist_id,))
        self._db.commit()

    def _ancestors(self, dist_id: int) -> list[tuple[int, str]]:
        """返回 [(祖先id, 该分销商在祖先的哪一侧), ...]，从近到远。"""
        result = []
        row = self._db.execute(
            "SELECT parent_id, side FROM distributors WHERE id=?",
            (dist_id,)).fetchone()
        parent, side = row
        while parent is not None:
            result.append((parent, side))
            parent, side = self._db.execute(
                "SELECT parent_id, side FROM distributors WHERE id=?",
                (parent,)).fetchone()
        return result

    def post_volume(self, dist_id: int, week: int, points: int,
                    order_ref: str) -> None:
        """记一条业绩（points 为负表示退货），归属到 week 名下。"""
        cur_week = self.current_week
        if week < 1 or week > cur_week:
            raise ValueError(f"week 必须在 1..{cur_week} 之间, 收到 {week}")
        exists = self._db.execute(
            "SELECT 1 FROM distributors WHERE id=?", (dist_id,)).fetchone()
        if exists is None:
            raise ValueError(f"分销商不存在: {dist_id}")
        self._db.execute(
            "INSERT INTO volumes(dist_id, week, points, order_ref)"
            " VALUES(?,?,?,?)", (dist_id, week, points, order_ref))
        # 本人当周业绩
        self._db.execute(
            "INSERT INTO week_vol(dist_id, week, personal) VALUES(?,?,?)"
            " ON CONFLICT(dist_id, week)"
            " DO UPDATE SET personal = personal + excluded.personal",
            (dist_id, week, points))
        # 逐层上线累加到对应侧的当周子树业绩
        ancestors = self._ancestors(dist_id)
        for anc_id, side in ancestors:
            col = "sub_l" if side == "L" else "sub_r"
            self._db.execute(
                f"INSERT INTO week_vol(dist_id, week, {col}) VALUES(?,?,?)"
                f" ON CONFLICT(dist_id, week)"
                f" DO UPDATE SET {col} = {col} + excluded.{col}",
                (anc_id, week, points))
        # 迟到业绩（属于已结算周）：把所属人及上线标脏，记录最早受影响周
        if week < cur_week:
            for d in [dist_id, *(a for a, _ in ancestors)]:
                self._db.execute(
                    "INSERT INTO dirty(dist_id, min_week) VALUES(?,?)"
                    " ON CONFLICT(dist_id) DO UPDATE SET"
                    " min_week = MIN(min_week, excluded.min_week)",
                    (d, week))
        self._db.commit()

    def settle(self) -> Batch:
        """结算当前未结周，生成并返回批次，未结周后移一周。"""
        db = self._db
        week = self.current_week
        dirty = dict(db.execute("SELECT dist_id, min_week FROM dirty"))

        # 批量加载重算所需数据：脏分销商从最早受影响周起的聚合与旧状态，
        # 以及全体分销商本周的聚合和上周的结转。
        vol: dict[tuple[int, int], tuple[int, int, int]] = {}
        state: dict[tuple[int, int], tuple[int, int, int]] = {}
        for d, w0 in dirty.items():
            for w, p, sl, sr in db.execute(
                    "SELECT week, personal, sub_l, sub_r FROM week_vol"
                    " WHERE dist_id=? AND week>=?", (d, w0)):
                vol[(d, w)] = (p, sl, sr)
            for w, b, cl, cr in db.execute(
                    "SELECT week, bonus, carry_l, carry_r FROM week_state"
                    " WHERE dist_id=? AND week>=?", (d, w0 - 1)):
                state[(d, w)] = (b, cl, cr)
        for d, p, sl, sr in db.execute(
                "SELECT dist_id, personal, sub_l, sub_r FROM week_vol"
                " WHERE week=?", (week,)):
            vol[(d, week)] = (p, sl, sr)
        if week > 1:
            for d, b, cl, cr in db.execute(
                    "SELECT dist_id, bonus, carry_l, carry_r FROM week_state"
                    " WHERE week=?", (week - 1,)):
                state.setdefault((d, week - 1), (b, cl, cr))

        all_dists = [r[0] for r in db.execute(
            "SELECT id FROM distributors ORDER BY id")]
        accounts = {d: (pt, debt)
                    for d, pt, debt in db.execute(
                        "SELECT dist_id, paid_total, debt FROM accounts")}

        touched: set[tuple[int, int]] = set()
        state_writes = []   # (dist_id, week, bonus, carry_l, carry_r)
        line_rows = []      # batch_lines 行
        account_rows = []   # accounts 更新
        for d in all_dists:
            start = dirty.get(d, week)
            _, carry_l, carry_r = state.get((d, start - 1), (0, 0, 0))
            old_sum = sum(
                state[(d, w)][0] for w in range(start, week)
                if (d, w) in state)
            new_old_sum = 0
            week_bonus = 0
            for w in range(start, week + 1):
                personal, sub_l, sub_r = vol.get((d, w), (0, 0, 0))
                left = carry_l + sub_l
                right = carry_r + sub_r
                if personal >= ACTIVE_THRESHOLD:
                    m = min(left, right)
                    bonus = min(m * BONUS_PER_POINT, WEEKLY_BONUS_CAP)
                    left -= m
                    right -= m
                else:
                    bonus = 0
                carry_l = min(left, CARRY_CAP)
                carry_r = min(right, CARRY_CAP)
                state_writes.append((d, w, bonus, carry_l, carry_r))
                if w < week:
                    touched.add((d, w))
                    new_old_sum += bonus
                else:
                    week_bonus = bonus
            adjustment = new_old_sum - old_sum
            delta = week_bonus + adjustment
            paid_total, debt = accounts[d]
            if delta >= 0:
                absorb = min(debt, delta)
                debt -= absorb
                paid = delta - absorb
            else:
                debt += -delta
                paid = 0
            paid_total += paid
            accounts[d] = (paid_total, debt)
            line_rows.append((week, d, week_bonus, adjustment, paid, debt))
            account_rows.append((paid_total, debt,
                                 paid_total - debt, d))

        db.executemany(
            "INSERT OR REPLACE INTO week_state"
            " (dist_id, week, bonus, carry_l, carry_r) VALUES(?,?,?,?,?)",
            state_writes)
        db.executemany(
            "INSERT INTO batch_lines"
            " (batch_no, dist_id, week_bonus, adjustment, paid, debt)"
            " VALUES(?,?,?,?,?,?)", line_rows)
        db.executemany(
            "UPDATE accounts SET paid_total=?, debt=?, cum_bonus=?"
            " WHERE dist_id=?", account_rows)
        db.execute("INSERT INTO batches(batch_no, week) VALUES(?,?)",
                   (week, week))
        db.execute("DELETE FROM dirty")
        db.execute("UPDATE meta SET value=? WHERE key='current_week'",
                   (week + 1,))
        db.commit()
        self.last_touched = touched
        return self.get_run(week)

    def get_run(self, n: int) -> Batch:
        """读第 n 个批次。"""
        row = self._db.execute(
            "SELECT week FROM batches WHERE batch_no=?", (n,)).fetchone()
        if row is None:
            raise KeyError(f"批次不存在: {n}")
        lines = tuple(
            Line(dist_id=d, week_bonus=wb, adjustment=adj, paid=p, debt=dbt)
            for d, wb, adj, p, dbt in self._db.execute(
                "SELECT dist_id, week_bonus, adjustment, paid, debt"
                " FROM batch_lines WHERE batch_no=? ORDER BY dist_id", (n,)))
        return Batch(batch_no=n, week=row[0], lines=lines)
