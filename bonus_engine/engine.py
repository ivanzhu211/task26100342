"""双轨制奖金周结算引擎。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .db import connect

ACTIVE_THRESHOLD = 100      # 本人周业绩活跃门槛（点）
RATE = 10                   # 每配对点奖金（分）
WEEKLY_CAP = 200_000        # 单周奖金封顶（分）
CARRY_CAP = 20_000          # 每侧最大结转（点）


@dataclass(frozen=True)
class BatchLine:
    distributor_id: int
    week_bonus: int      # 本周奖金（分）
    adjustment: int      # 对旧周的调整（分，可为负）
    paid: int            # 本次实发（分，>=0）
    debt: int            # 结算后欠款（分，>=0）


@dataclass(frozen=True)
class Batch:
    id: int
    week: int
    lines: tuple[BatchLine, ...]

    def line_for(self, distributor_id: int) -> BatchLine:
        for line in self.lines:
            if line.distributor_id == distributor_id:
                return line
        raise KeyError(distributor_id)


class Engine:
    def __init__(self, db_path: str) -> None:
        self._conn = connect(db_path)
        # 观测钩子：上一次 settle 重算阶段实际触及的 (分销商, 周)
        self.last_recompute: set[tuple[int, int]] = set()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def current_week(self) -> int:
        """当前未结周号（从 1 开始）。"""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='current_week'"
        ).fetchone()
        return int(row[0])

    def _set_current_week(self, week: int) -> None:
        self._conn.execute(
            "UPDATE meta SET value=? WHERE key='current_week'", (week,)
        )

    # ---------------------------------------------------------------- enroll

    def enroll(self, distributor_id: int, parent_id: Optional[int] = None,
               side: Optional[str] = None) -> None:
        cur = self._conn
        if cur.execute("SELECT 1 FROM distributors WHERE id=?",
                       (distributor_id,)).fetchone():
            raise ValueError(f"distributor {distributor_id} already enrolled")
        if parent_id is None:
            if side is not None:
                raise ValueError("root has no side")
            if cur.execute("SELECT 1 FROM distributors "
                           "WHERE parent_id IS NULL").fetchone():
                raise ValueError("root already exists")
            cur.execute(
                "INSERT INTO distributors(id, parent_id, side) VALUES(?,?,?)",
                (distributor_id, None, None))
        else:
            if side not in ("L", "R"):
                raise ValueError("side must be 'L' or 'R'")
            if not cur.execute("SELECT 1 FROM distributors WHERE id=?",
                               (parent_id,)).fetchone():
                raise ValueError(f"parent {parent_id} not enrolled")
            if cur.execute(
                    "SELECT 1 FROM distributors WHERE parent_id=? AND side=?",
                    (parent_id, side)).fetchone():
                raise ValueError(f"side {side} of parent {parent_id} occupied")
            cur.execute(
                "INSERT INTO distributors(id, parent_id, side) VALUES(?,?,?)",
                (distributor_id, parent_id, side))
        cur.commit()

    # ----------------------------------------------------------- post_volume

    def _ancestors(self, distributor_id: int):
        """yield (ancestor_id, side)：distributor 挂在 ancestor 的哪一侧。"""
        cur = self._conn
        node = distributor_id
        while True:
            row = cur.execute(
                "SELECT parent_id, side FROM distributors WHERE id=?",
                (node,)).fetchone()
            if row is None or row[0] is None:
                return
            yield row[0], row[1]
            node = row[0]

    def post_volume(self, distributor_id: int, week: int, points: int,
                    order_ref: str) -> None:
        cur = self._conn
        if not 1 <= week <= self.current_week:
            raise ValueError(f"invalid week {week}")
        if not cur.execute("SELECT 1 FROM distributors WHERE id=?",
                           (distributor_id,)).fetchone():
            raise ValueError(f"distributor {distributor_id} not enrolled")
        late = week < self.current_week
        with cur:  # 单事务：流水 + 聚合 + 脏标记
            cur.execute(
                "INSERT INTO volumes(distributor_id, week, points, order_ref)"
                " VALUES(?,?,?,?)",
                (distributor_id, week, points, order_ref))
            self._add_own(distributor_id, week, points)
            for ancestor_id, side in self._ancestors(distributor_id):
                self._add_side(ancestor_id, week, side, points)
                if late:
                    self._mark_dirty(ancestor_id, week)
            if late:
                self._mark_dirty(distributor_id, week)

    @staticmethod
    def _upsert_sql(column: str) -> str:
        return (
            f"INSERT INTO week_state(distributor_id, week, {column})"
            f" VALUES(?,?,?)"
            f" ON CONFLICT(distributor_id, week)"
            f" DO UPDATE SET {column} = {column} + excluded.{column}"
        )

    def _add_own(self, distributor_id: int, week: int, points: int) -> None:
        self._conn.execute(self._upsert_sql("own"),
                           (distributor_id, week, points))

    def _add_side(self, distributor_id: int, week: int, side: str,
                  points: int) -> None:
        column = "left_v" if side == "L" else "right_v"
        self._conn.execute(self._upsert_sql(column),
                           (distributor_id, week, points))

    def _mark_dirty(self, distributor_id: int, week: int) -> None:
        self._conn.execute(
            "INSERT INTO dirty(distributor_id, earliest_week) VALUES(?,?)"
            " ON CONFLICT(distributor_id) DO UPDATE SET"
            " earliest_week = MIN(earliest_week, excluded.earliest_week)",
            (distributor_id, week))

    @staticmethod
    def _compute_week(own: int, left: int, right: int,
                      ci_l: int, ci_r: int) -> tuple[int, int, int]:
        """单周计算：返回 (bonus, carry_out_l, carry_out_r)。

        L = 左结转 + 左子树当周业绩；R 同理。活跃（own >= 100）时
        m = min(L, R)，奖金 min(m*10, 200000)；两侧各扣 m（封顶截掉
        的部分照样扣）。每侧结转上限 20000，多出作废。
        """
        total_l = ci_l + left
        total_r = ci_r + right
        if own >= ACTIVE_THRESHOLD:
            m = min(total_l, total_r)
            bonus = min(m * RATE, WEEKLY_CAP)
        else:
            m = 0
            bonus = 0
        co_l = min(total_l - m, CARRY_CAP)
        co_r = min(total_r - m, CARRY_CAP)
        return bonus, co_l, co_r

    # ---------------------------------------------------------------- settle

    def _recompute_dirty(self, current_week: int) -> dict[int, int]:
        """重算脏分销商的旧周（earliest_week..current_week-1）。

        只触及 dirty 表中的分销商（迟到业绩所属人及其上线），
        每人只从最早受影响周开始。返回 {分销商: 对旧周的调整合计}。
        """
        cur = self._conn
        adjustments: dict[int, int] = {}
        dirty = cur.execute(
            "SELECT distributor_id, earliest_week FROM dirty"
        ).fetchall()
        for dist_id, start in dirty:
            if start >= current_week:
                continue
            rows = {
                r[0]: r[1:]
                for r in cur.execute(
                    "SELECT week, own, left_v, right_v, bonus, co_l, co_r"
                    " FROM week_state"
                    " WHERE distributor_id=? AND week>=? AND week<?",
                    (dist_id, start, current_week))
            }
            ci_l = ci_r = 0
            if start > 1:
                prev = cur.execute(
                    "SELECT co_l, co_r FROM week_state"
                    " WHERE distributor_id=? AND week=?",
                    (dist_id, start - 1)).fetchone()
                if prev:
                    ci_l, ci_r = prev
            row_weeks = sorted(rows)
            ptr = 0
            week = start
            while week < current_week:
                row = rows.get(week)
                if row is None and ci_l == 0 and ci_r == 0:
                    # 无行业绩且无结转：结果必为全零，跳到下一个有行的周
                    while ptr < len(row_weeks) and row_weeks[ptr] <= week:
                        ptr += 1
                    if ptr >= len(row_weeks):
                        break
                    week = row_weeks[ptr]
                    continue
                own, lv, rv = (row[0], row[1], row[2]) if row else (0, 0, 0)
                old_bonus = row[3] if row else 0
                bonus, co_l, co_r = self._compute_week(own, lv, rv,
                                                       ci_l, ci_r)
                self.last_recompute.add((dist_id, week))
                if bonus != old_bonus:
                    adjustments[dist_id] = (adjustments.get(dist_id, 0)
                                            + bonus - old_bonus)
                if row is not None or bonus or co_l or co_r:
                    cur.execute(
                        "INSERT INTO week_state(distributor_id, week,"
                        " own, left_v, right_v, bonus, co_l, co_r)"
                        " VALUES(?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(distributor_id, week) DO UPDATE SET"
                        " bonus=excluded.bonus, co_l=excluded.co_l,"
                        " co_r=excluded.co_r",
                        (dist_id, week, own, lv, rv, bonus, co_l, co_r))
                ci_l, ci_r = co_l, co_r
                week += 1
        return adjustments

    def settle(self) -> Batch:
        cur = self._conn
        w = self.current_week
        self.last_recompute = set()
        with cur:  # 整个结算批一个事务
            adjustments = self._recompute_dirty(w)

            carry = {}
            if w > 1:
                carry = {r[0]: (r[1], r[2]) for r in cur.execute(
                    "SELECT distributor_id, co_l, co_r FROM week_state"
                    " WHERE week=?", (w - 1,))}
            vol = {r[0]: (r[1], r[2], r[3]) for r in cur.execute(
                "SELECT distributor_id, own, left_v, right_v"
                " FROM week_state WHERE week=?", (w,))}
            dist_ids = [r[0] for r in cur.execute(
                "SELECT id FROM distributors ORDER BY id")]
            debts = {r[0]: r[1] for r in cur.execute(
                "SELECT distributor_id, debt FROM debts")}

            cur.execute("INSERT INTO batches(id, week) VALUES(?,?)", (w, w))
            lines = []
            for dist_id in dist_ids:
                own, lv, rv = vol.get(dist_id, (0, 0, 0))
                ci_l, ci_r = carry.get(dist_id, (0, 0))
                bonus, co_l, co_r = self._compute_week(own, lv, rv,
                                                       ci_l, ci_r)
                if own or lv or rv or bonus or co_l or co_r:
                    cur.execute(
                        "INSERT INTO week_state(distributor_id, week,"
                        " own, left_v, right_v, bonus, co_l, co_r)"
                        " VALUES(?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(distributor_id, week) DO UPDATE SET"
                        " bonus=excluded.bonus, co_l=excluded.co_l,"
                        " co_r=excluded.co_r",
                        (dist_id, w, own, lv, rv, bonus, co_l, co_r))
                adjustment = adjustments.get(dist_id, 0)
                entitled = bonus + adjustment
                debt_old = debts.get(dist_id, 0)
                paid = max(0, entitled - debt_old)
                debt_new = max(0, debt_old - entitled)
                cur.execute(
                    "INSERT INTO batch_lines(batch_id, distributor_id,"
                    " week_bonus, adjustment, paid, debt)"
                    " VALUES(?,?,?,?,?,?)",
                    (w, dist_id, bonus, adjustment, paid, debt_new))
                if debt_new:
                    cur.execute(
                        "INSERT INTO debts(distributor_id, debt)"
                        " VALUES(?,?)"
                        " ON CONFLICT(distributor_id)"
                        " DO UPDATE SET debt=excluded.debt",
                        (dist_id, debt_new))
                else:
                    cur.execute(
                        "DELETE FROM debts WHERE distributor_id=?",
                        (dist_id,))
                lines.append(BatchLine(dist_id, bonus, adjustment,
                                       paid, debt_new))
            cur.execute("DELETE FROM dirty")
            self._set_current_week(w + 1)
        return Batch(w, w, tuple(lines))

    def get_run(self, n: int) -> Batch:
        cur = self._conn
        row = cur.execute("SELECT id, week FROM batches WHERE id=?",
                          (n,)).fetchone()
        if row is None:
            raise KeyError(f"batch {n} not found")
        lines = tuple(
            BatchLine(*r) for r in cur.execute(
                "SELECT distributor_id, week_bonus, adjustment, paid, debt"
                " FROM batch_lines WHERE batch_id=?"
                " ORDER BY distributor_id", (n,)))
        return Batch(row[0], row[1], lines)
