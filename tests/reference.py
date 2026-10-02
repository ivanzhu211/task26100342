"""独立参照实现：每次调用都用当前已知全部业绩，从第 1 周逐周模拟。

与被测引擎不共享任何代码路径，用于核对不变式：
    历次实发之和 - 当前欠款 == 全量重算的累计奖金 S
"""
from __future__ import annotations

ACTIVE_THRESHOLD = 100
RATE = 10
WEEKLY_CAP = 200_000
CARRY_CAP = 20_000


class Reference:
    def __init__(self) -> None:
        self.parent: dict[int, tuple[int, str]] = {}  # id -> (parent, side)
        self.members: list[int] = []
        self.volumes: list[tuple[int, int, int]] = []  # (dist, week, points)

    def enroll(self, dist_id: int, parent_id: int | None,
               side: str | None) -> None:
        self.members.append(dist_id)
        if parent_id is not None:
            self.parent[dist_id] = (parent_id, side)

    def post_volume(self, dist_id: int, week: int, points: int) -> None:
        self.volumes.append((dist_id, week, points))

    def cumulative_bonuses(self, last_week: int) -> dict[int, int]:
        """用全部已知业绩从第 1 周算到 last_week，返回每人累计奖金。"""
        own: dict[tuple[int, int], int] = {}
        side_vol: dict[tuple[int, int], int] = {}  # (ancestor, week) -> L/R
        left: dict[tuple[int, int], int] = {}
        right: dict[tuple[int, int], int] = {}
        for dist, week, points in self.volumes:
            if week > last_week:
                continue
            key = (dist, week)
            own[key] = own.get(key, 0) + points
            node = dist
            while node in self.parent:
                anc, side = self.parent[node]
                table = left if side == "L" else right
                akey = (anc, week)
                table[akey] = table.get(akey, 0) + points
                node = anc
        result: dict[int, int] = {}
        for dist in self.members:
            ci_l = ci_r = 0
            total = 0
            for week in range(1, last_week + 1):
                key = (dist, week)
                total_l = ci_l + left.get(key, 0)
                total_r = ci_r + right.get(key, 0)
                if own.get(key, 0) >= ACTIVE_THRESHOLD:
                    m = min(total_l, total_r)
                    total += min(m * RATE, WEEKLY_CAP)
                else:
                    m = 0
                ci_l = min(total_l - m, CARRY_CAP)
                ci_r = min(total_r - m, CARRY_CAP)
            result[dist] = total
        return result
