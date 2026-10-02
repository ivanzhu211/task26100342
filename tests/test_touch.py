"""只有一条迟到业绩时，重算实际触及的分销商和周不超出其所属人及上线、
且不早于那一周。"""

from bonus_engine import Engine

from .reference import Reference


def test_minimal_recompute_scope(tmp_path):
    eng = Engine(str(tmp_path / "s.db"))
    ref = Reference()
    # 树：
    #        1
    #      /   \
    #     2     3
    #    / \   /
    #   4   5 6
    for d, p, s in [(1, None, None), (2, 1, "L"), (3, 1, "R"),
                    (4, 2, "L"), (5, 2, "R"), (6, 3, "L")]:
        eng.enroll(d, p, s)
        ref.enroll(d, p, s)

    # 前 3 周正常下单并结算
    for week in range(1, 4):
        for d in (1, 2, 3, 4, 5, 6):
            eng.post_volume(d, week, 100 + 10 * d, f"w{week}d{d}")
            ref.post_volume(d, week, 100 + 10 * d)
        eng.settle()
        ref.settle()

    # 一条迟到业绩：4号第2周 +500。受影响上线链：4 -> 2 -> 1
    eng.post_volume(4, 2, 500, "late")
    ref.post_volume(4, 2, 500)
    eng.settle()   # 结算第4周
    ref.settle()

    expected_touched = {(d, w) for d in (4, 2, 1) for w in (2, 3)}
    assert eng.last_touched == expected_touched
    # 不早于第2周、不触及非上线（3、5、6 及其子树）
    assert all(w >= 2 for _, w in eng.last_touched)
    assert all(d in (4, 2, 1) for d, _ in eng.last_touched)

    # 结果仍与全量重放一致
    batch = eng.get_run(4)
    for line in batch.lines:
        assert line.paid >= 0 and line.debt >= 0
    paid_sum = {d: 0 for d in (1, 2, 3, 4, 5, 6)}
    debt = {}
    for n in range(1, 5):
        for line in eng.get_run(n).lines:
            paid_sum[line.dist_id] += line.paid
            debt[line.dist_id] = line.debt
    for d in paid_sum:
        assert paid_sum[d] - debt[d] == ref.last_cum[d]
    eng.close()


def test_no_late_volume_touches_nothing(tmp_path):
    eng = Engine(str(tmp_path / "s.db"))
    eng.enroll(1)
    eng.enroll(2, 1, "L")
    eng.post_volume(2, 1, 500, "a")
    eng.settle()
    eng.post_volume(2, 2, 500, "b")  # 本周业绩，不算迟到
    eng.settle()
    assert eng.last_touched == set()
    eng.close()
