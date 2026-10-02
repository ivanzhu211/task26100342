"""独立参照实现：纯内存，每次结算都从第 1 周逐周全量重放。

故意不复用引擎的任何代码，用来交叉验证引擎的增量重算与财务等式。
"""

from __future__ import annotations

ACTIVE_THRESHOLD = 100
BONUS_PER_POINT = 10
WEEKLY_BONUS_CAP = 200_000
CARRY_CAP = 20_000


class Reference:
    def __init__(self) -> None:
        self.parent: dict[int, tuple[int, str] | None] = {}
        self.volumes: list[tuple[int, int, int]] = []  # (dist, week, points)
        self.week = 1
        self.paid_total: dict[int, int] = {}
        self.debt: dict[int, int] = {}
        self.last_cum: dict[int, int] = {}

    def enroll(self, dist_id, parent_id=None, side=None):
        self.parent[dist_id] = None if parent_id is None else (parent_id, side)
        self.paid_total[dist_id] = 0
        self.debt[dist_id] = 0

    def post_volume(self, dist_id, week, points):
        self.volumes.append((dist_id, week, points))

    def _ancestors(self, dist_id):
        result = []
        link = self.parent[dist_id]
        while link is not None:
            result.append(link)
            link = self.parent[link[0]]
        return result

    def settle(self):
        """从第 1 周重放到当前周。

        返回 {dist: (本周奖金, 对旧周调整, 本次实发, 结算后欠款)}。
        """
        personal: dict[tuple[int, int], int] = {}
        sub: dict[tuple[int, int], list[int]] = {}
        for d, w, p in self.volumes:
            personal[(d, w)] = personal.get((d, w), 0) + p
            for anc, side in self._ancestors(d):
                key = (anc, w)
                if key not in sub:
                    sub[key] = [0, 0]
                sub[key][0 if side == "L" else 1] += p

        carry = {d: [0, 0] for d in self.parent}
        cum = {d: 0 for d in self.parent}      # S：用当前全部已知业绩算出的累计奖金
        week_bonus = {d: 0 for d in self.parent}
        for w in range(1, self.week + 1):
            for d in self.parent:
                cl, cr = carry[d]
                sl, sr = sub.get((d, w), (0, 0))
                left, right = cl + sl, cr + sr
                if personal.get((d, w), 0) >= ACTIVE_THRESHOLD:
                    m = min(left, right)
                    bonus = min(m * BONUS_PER_POINT, WEEKLY_BONUS_CAP)
                    left -= m
                    right -= m
                else:
                    bonus = 0
                carry[d] = [min(left, CARRY_CAP), min(right, CARRY_CAP)]
                cum[d] += bonus
                if w == self.week:
                    week_bonus[d] = bonus

        self.last_cum = cum
        result = {}
        for d in self.parent:
            delta = cum[d] - (self.paid_total[d] - self.debt[d])
            if delta >= 0:
                absorb = min(self.debt[d], delta)
                self.debt[d] -= absorb
                paid = delta - absorb
            else:
                self.debt[d] += -delta
                paid = 0
            self.paid_total[d] += paid
            adjustment = delta - week_bonus[d]
            result[d] = (week_bonus[d], adjustment, paid, self.debt[d])
        self.week += 1
        return result
