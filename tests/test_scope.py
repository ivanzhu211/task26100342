"""重算范围：单条迟到业绩只触及所属人及上线、不早于其所属周。"""
import os
import tempfile
import unittest

from bonus_engine import Engine


class RecomputeScopeTest(unittest.TestCase):
    def test_scope_limited_to_owner_and_upline(self):
        tmp = tempfile.mkdtemp()
        engine = Engine(os.path.join(tmp, "state.db"))
        # 主链 1-2-3-4（全挂左侧），旁支 5、6 不在 4 的上线路径上
        engine.enroll(1)
        engine.enroll(2, 1, "L")
        engine.enroll(3, 2, "L")
        engine.enroll(4, 3, "L")
        engine.enroll(5, 1, "R")
        engine.enroll(6, 2, "R")

        # 跑 5 周，每周大家都有点业绩，制造结转状态
        for week in range(1, 6):
            for dist in (1, 2, 3, 4, 5, 6):
                engine.post_volume(dist, week, 150, f"w{week}d{dist}")
            engine.settle()

        # 一条迟到业绩：分销商 4、第 3 周
        engine.post_volume(4, 3, 500, "late")
        engine.settle()

        touched = engine.last_recompute
        self.assertTrue(touched, "迟到业绩应触发重算")
        allowed_dists = {4, 3, 2, 1}  # 所属人及全部上线
        for dist, week in touched:
            self.assertIn(dist, allowed_dists,
                          f"分销商 {dist} 不在所属人及上线范围内")
            self.assertGreaterEqual(week, 3, "重算周早于迟到业绩所属周")
            self.assertLessEqual(week, 5, "重算周超过上次已结周")
        # 确实重算了所属人的所属周
        self.assertIn((4, 3), touched)
        # 旁支未被触及
        self.assertFalse({5, 6} & {d for d, _ in touched})
        engine.close()


if __name__ == "__main__":
    unittest.main()
