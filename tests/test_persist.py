"""关掉再打开能接着结算，且迟到业绩重述在重开前后都正确。"""

from bonus_engine import Engine

from .reference import Reference


def test_reopen_and_continue(tmp_path):
    path = str(tmp_path / "s.db")
    ref = Reference()
    eng = Engine(path)
    eng.enroll(1)
    eng.enroll(2, 1, "L")
    eng.enroll(3, 1, "R")
    ref.enroll(1)
    ref.enroll(2, 1, "L")
    ref.enroll(3, 1, "R")

    for week in (1, 2):
        eng.post_volume(1, week, 100, f"p{week}")
        eng.post_volume(2, week, 5000, f"l{week}")
        eng.post_volume(3, week, 5000, f"r{week}")
        ref.post_volume(1, week, 100)
        ref.post_volume(2, week, 5000)
        ref.post_volume(3, week, 5000)
        eng.settle()
        ref.settle()
    eng.close()

    # 重开：当前未结周应是第3周，旧批次仍在
    eng = Engine(path)
    assert eng.current_week == 3
    assert eng.get_run(2).line_for(1).paid > 0

    # 重开后记迟到业绩并继续结算，结果与参照一致
    eng.post_volume(2, 1, 7000, "late")
    ref.post_volume(2, 1, 7000)
    eng.post_volume(1, 3, 100, "p3")
    ref.post_volume(1, 3, 100)
    batch = eng.settle()
    expected = ref.settle()
    for line in batch.lines:
        assert (line.week_bonus, line.adjustment, line.paid, line.debt) \
            == expected[line.dist_id]
    eng.close()

    # 再次重开，批次内容依旧
    eng = Engine(path)
    assert eng.current_week == 4
    assert eng.get_run(3) == batch
    eng.close()
