"""定向场景：迟到业绩引发连锁重述；退货追回额跨批次扣完。"""
import os
import tempfile
import unittest

from bonus_engine import Engine
from tests.reference import Reference


class ChainReactionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "state.db")
        self.engine = Engine(self.db)
        self.ref = Reference()
        # X=1 为根，左子 P=2，右子 Q=3
        self.engine.enroll(1)
        self.engine.enroll(2, 1, "L")
        self.engine.enroll(3, 1, "R")
        self.ref.enroll(1, None, None)
        self.ref.enroll(2, 1, "L")
        self.ref.enroll(3, 1, "R")

    def tearDown(self):
        self.engine.close()

    def post(self, dist, week, points, ref_no):
        self.engine.post_volume(dist, week, points, ref_no)
        self.ref.post_volume(dist, week, points)

    def settle_and_check(self, week):
        batch = self.engine.settle()
        self.assertEqual(batch.week, week)
        expected = self.ref.cumulative_bonuses(week)
        paid_sum = {d: 0 for d in (1, 2, 3)}
        debt_now = {}
        for n in range(1, week + 1):
            for line in self.engine.get_run(n).lines:
                paid_sum[line.distributor_id] += line.paid
                debt_now[line.distributor_id] = line.debt
        for d in (1, 2, 3):
            self.assertEqual(paid_sum[d] - debt_now[d], expected[d])
        return batch

    def week_bonuses(self, dist, weeks):
        rows = self.engine._conn.execute(
            "SELECT week, bonus FROM week_state"
            " WHERE distributor_id=? AND week IN (%s) ORDER BY week"
            % ",".join("?" * len(weeks)),
            (dist, *weeks)).fetchall()
        return {w: b for w, b in rows}

    def test_late_activation_cascades(self):
        # 第1周：两侧各 25000，X 本人无业绩 -> 不活跃，结转 20000/20000
        self.post(2, 1, 25000, "a")
        self.post(3, 1, 25000, "b")
        b1 = self.settle_and_check(1)
        self.assertEqual(b1.line_for(1).week_bonus, 0)

        # 第2周：X 活跃，L=R=20000+10000=30000 -> 封顶 200000
        self.post(1, 2, 100, "c")
        self.post(2, 2, 10000, "d")
        self.post(3, 2, 10000, "e")
        b2 = self.settle_and_check(2)
        self.assertEqual(b2.line_for(1).week_bonus, 200000)
        self.assertEqual(b2.line_for(1).paid, 200000)

        # 第3周：X 活跃但两侧为空 -> 0
        self.post(1, 3, 100, "f")
        b3 = self.settle_and_check(3)
        self.assertEqual(b3.line_for(1).week_bonus, 0)

        # 迟到业绩：X 第1周本人 +100 -> 第1周变活跃
        self.post(1, 1, 100, "g")
        b4 = self.settle_and_check(4)

        # 连锁结果：w1 0->200000(25000 配对被封顶)，结转 20000/20000->0/0；
        # w2 200000->100000（L=R=10000）；w3 0->0
        self.assertEqual(self.week_bonuses(1, [1, 2, 3]),
                         {1: 200000, 2: 100000, 3: 0})
        line = b4.line_for(1)
        self.assertEqual(line.adjustment, 100000)
        self.assertEqual(line.paid, 100000)
        self.assertEqual(line.debt, 0)

    def test_clawback_spans_batches(self):
        # 第1周：Y 活跃，两侧各 350 -> 奖金 3500
        self.post(2, 1, 350, "a")
        self.post(3, 1, 350, "b")
        self.post(1, 1, 100, "c")
        b1 = self.settle_and_check(1)
        self.assertEqual(b1.line_for(1).paid, 3500)

        # 退货：P 第1周订单全额退回 -> 第1周 L=0，奖金 3500->0
        self.post(2, 1, -350, "a")

        # 第2..5周：每周两侧各 100、Y 本人 100 -> 每周应发 1000
        def post_week(week):
            self.post(2, week, 100, f"p{week}")
            self.post(3, week, 100, f"q{week}")
            self.post(1, week, 100, f"y{week}")

        # 第2批：调整 -3500，应发 1000-3500=-2500 -> 实发 0，欠款 2500
        post_week(2)
        b2 = self.settle_and_check(2)
        self.assertEqual(b2.line_for(1).adjustment, -3500)
        self.assertEqual(b2.line_for(1).paid, 0)
        self.assertEqual(b2.line_for(1).debt, 2500)

        # 追回额 2500 超过随后两周应发（2000），欠款跨批次扣
        post_week(3)
        b3 = self.settle_and_check(3)
        self.assertEqual((b3.line_for(1).paid, b3.line_for(1).debt), (0, 1500))
        post_week(4)
        b4 = self.settle_and_check(4)
        self.assertEqual((b4.line_for(1).paid, b4.line_for(1).debt), (0, 500))

        # 第5批：应发 1000 抵扣欠款 500 后实发 500，欠款清零
        post_week(5)
        b5 = self.settle_and_check(5)
        self.assertEqual((b5.line_for(1).paid, b5.line_for(1).debt), (500, 0))


if __name__ == "__main__":
    unittest.main()
