"""随机树 + 大量迟到业绩/退货：每次 settle 后核对不变式与批次不可变性。"""
import os
import random
import tempfile
import unittest

from bonus_engine import Engine
from tests.reference import Reference


class RandomFuzzTest(unittest.TestCase):
    def test_fuzz_with_late_volume_and_returns(self):
        rng = random.Random(20261003)
        n_nodes, n_weeks = 300, 30
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "state.db")

        engine = Engine(db_path)
        ref = Reference()
        # 随机二叉树：逐个挂到随机已有节点的空侧
        engine.enroll(1)
        ref.enroll(1, None, None)
        open_slots = [(1, "L"), (1, "R")]
        for dist_id in range(2, n_nodes + 1):
            idx = rng.randrange(len(open_slots))
            parent, side = open_slots.pop(idx)
            engine.enroll(dist_id, parent, side)
            ref.enroll(dist_id, parent, side)
            open_slots.append((dist_id, "L"))
            open_slots.append((dist_id, "R"))

        orders: dict[str, list] = {}  # ref -> [dist, week, cumulative]
        order_seq = 0
        paid_sum: dict[int, int] = {d: 0 for d in range(1, n_nodes + 1)}
        debt_now: dict[int, int] = {d: 0 for d in range(1, n_nodes + 1)}
        snapshots = {}  # batch_id -> tuple(lines)

        def post(dist, week, points):
            nonlocal order_seq
            order_seq += 1
            ref_no = f"o{order_seq}"
            engine.post_volume(dist, week, points, ref_no)
            ref.post_volume(dist, week, points)
            orders[ref_no] = [dist, week, points]

        for week in range(1, n_weeks + 1):
            # 当周业绩
            for _ in range(rng.randint(20, 60)):
                post(rng.randint(1, n_nodes), week,
                     rng.choice([50, 100, 200, 500, 1000, 5000]))
            # 迟到业绩（已结算过的周；个别回溯到第 1 周）
            if week > 1:
                for _ in range(rng.randint(5, 25)):
                    late_week = (rng.randint(1, week - 1)
                                 if rng.random() < 0.15
                                 else max(1, week - rng.randint(1, 3)))
                    post(rng.randint(1, n_nodes), late_week,
                         rng.choice([100, 300, 800, 2000]))
            # 退货：挂在原订单那一周，累计不为负
            for _ in range(rng.randint(0, 8)):
                candidates = [k for k, v in orders.items() if v[2] > 0]
                if not candidates:
                    break
                ref_no = rng.choice(candidates)
                dist, w0, cum = orders[ref_no]
                amount = rng.randint(1, cum)
                engine.post_volume(dist, w0, -amount, ref_no)
                ref.post_volume(dist, w0, -amount)
                orders[ref_no][2] -= amount

            batch = engine.settle()
            self.assertEqual(batch.id, week)
            for line in batch.lines:
                paid_sum[line.distributor_id] += line.paid
                debt_now[line.distributor_id] = line.debt
                self.assertGreaterEqual(line.paid, 0)
                if line.debt > 0:
                    self.assertEqual(line.paid, 0)
            snapshots[week] = batch.lines

            # 硬性等式：历次实发之和 - 当前欠款 == 全量重算 S
            expected = ref.cumulative_bonuses(week)
            for dist in range(1, n_nodes + 1):
                self.assertEqual(
                    paid_sum[dist] - debt_now[dist], expected[dist],
                    f"week {week} dist {dist}")

            # 中途开关一次，验证持久化续算
            if week == n_weeks // 2:
                engine.close()
                engine = Engine(db_path)
                self.assertEqual(engine.current_week, week + 1)

        # 所有旧批次逐字段未变
        for batch_id, lines in snapshots.items():
            rerun = engine.get_run(batch_id)
            self.assertEqual(rerun.week, batch_id)
            self.assertEqual(rerun.lines, lines)

        engine.close()


if __name__ == "__main__":
    unittest.main()
