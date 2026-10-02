"""迟到业绩把旧周从不活跃翻成活跃，连锁改变结转与封顶；
随后一笔退货使追回额超过随后两周应发，欠款跨批次扣完。"""

from bonus_engine import Engine

from .reference import Reference


def test_chain_restate_and_debt_clawback(tmp_path):
    eng = Engine(str(tmp_path / "s.db"))
    ref = Reference()
    eng.enroll(1)
    eng.enroll(2, 1, "L")
    eng.enroll(3, 1, "R")
    ref.enroll(1)
    ref.enroll(2, 1, "L")
    ref.enroll(3, 1, "R")

    def post(dist, week, points, tag):
        eng.post_volume(dist, week, points, tag)
        ref.post_volume(dist, week, points)

    def settle_week():
        """结算一周，返回 1 号的行，并与参照实现全量核对。"""
        batch = eng.settle()
        expected = ref.settle()
        for line in batch.lines:
            assert (line.week_bonus, line.adjustment, line.paid, line.debt) \
                == expected[line.dist_id]
        return batch.line_for(1)

    # 第1周：左右子树各 15000，1号本人 50 -> 不活跃，两侧各结转 15000
    post(2, 1, 15000, "A")
    post(3, 1, 15000, "B")
    post(1, 1, 50, "C1")
    settle_week()
    line = eng.get_run(1).line_for(1)
    assert (line.week_bonus, line.adjustment, line.paid, line.debt) == (0, 0, 0, 0)

    # 第2周：左右再各 15000，1号本人 100 -> 活跃
    # L=R=30000，配对 30000，奖金被封顶截到 200000，两侧扣光
    post(2, 2, 15000, "D")
    post(3, 2, 15000, "E")
    post(1, 2, 100, "F")
    settle_week()
    line = eng.get_run(2).line_for(1)
    assert (line.week_bonus, line.adjustment, line.paid, line.debt) == \
        (200000, 0, 200000, 0)

    # 迟到业绩：1号第1周 +60 -> 第1周变为活跃（110 >= 100）
    # 连锁改变：第1周配对 15000（不再结转），第2周从封顶 200000 变为 150000
    post(1, 1, 60, "G")
    settle_week()  # 结算第3周
    line = eng.get_run(3).line_for(1)
    assert (line.week_bonus, line.adjustment, line.paid, line.debt) == \
        (0, 100000, 100000, 0)

    # 退货：1号第1周 -60 -> 第1周回到不活跃，上述连锁全部还原
    # 追回 100000，形成欠款
    post(1, 1, -60, "G")
    settle_week()  # 结算第4周
    line = eng.get_run(4).line_for(1)
    assert (line.week_bonus, line.adjustment, line.paid, line.debt) == \
        (0, -100000, 0, 100000)

    # 随后每周 1号应发 30000：欠款 100000 超过两周应发，跨批次扣完
    for week, expect_debt, expect_paid in [
            (5, 70000, 0), (6, 40000, 0), (7, 10000, 0), (8, 0, 20000)]:
        post(1, week, 100, f"H{week}")
        post(2, week, 3000, f"L{week}")
        post(3, week, 3000, f"R{week}")
        settle_week()
        line = eng.get_run(week).line_for(1)
        assert (line.week_bonus, line.adjustment, line.paid, line.debt) == \
            (30000, 0, expect_paid, expect_debt)
        if expect_debt > 0:
            assert line.paid == 0

    # 硬性等式：历次实发之和 - 当前欠款 == 全量重放累计奖金 S
    paid_sum = sum(
        eng.get_run(n).line_for(1).paid for n in range(1, 9))
    assert paid_sum - eng.get_run(8).line_for(1).debt == ref.last_cum[1]
    eng.close()
