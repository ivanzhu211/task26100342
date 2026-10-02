import pytest

from bonus_engine import Engine


def test_placeholder():
    assert Engine is not None


def test_enroll_and_post(tmp_path):
    eng = Engine(str(tmp_path / "s.db"))
    eng.enroll(1)
    eng.enroll(2, 1, "L")
    eng.enroll(3, 1, "R")
    with pytest.raises(ValueError):
        eng.enroll(4, 1, "L")  # 左侧已占用
    with pytest.raises(ValueError):
        eng.enroll(5, 99, "L")  # 父节点不存在
    eng.post_volume(2, 1, 150, "o1")
    eng.post_volume(3, 1, 80, "o2")
    rows = dict(
        (r[0], (r[1], r[2], r[3]))
        for r in eng._db.execute(
            "SELECT dist_id, personal, sub_l, sub_r FROM week_vol"))
    assert rows[1] == (0, 150, 80)
    assert rows[2] == (150, 0, 0)
    assert rows[3] == (80, 0, 0)
    with pytest.raises(ValueError):
        eng.post_volume(2, 2, 10, "o3")  # 超过当前未结周
    eng.close()


def test_reopen_keeps_week(tmp_path):
    path = str(tmp_path / "s.db")
    eng = Engine(path)
    eng.enroll(1)
    assert eng.current_week == 1
    eng.close()
    eng2 = Engine(path)
    assert eng2.current_week == 1
    eng2.close()


def test_settle_basic(tmp_path):
    eng = Engine(str(tmp_path / "s.db"))
    eng.enroll(1)
    eng.enroll(2, 1, "L")
    eng.enroll(3, 1, "R")
    eng.post_volume(2, 1, 150, "o1")
    eng.post_volume(3, 1, 80, "o2")
    b1 = eng.settle()
    assert b1.batch_no == 1 and b1.week == 1
    # 第1周：1号本人无业绩不活跃；2、3号无子树，配对量为0
    assert all(line.paid == 0 and line.week_bonus == 0 for line in b1.lines)
    eng.post_volume(1, 2, 100, "o3")
    eng.post_volume(2, 2, 200, "o4")
    eng.post_volume(3, 2, 300, "o5")
    b2 = eng.settle()
    l1 = b2.line_for(1)
    # 1号活跃；上周未配对结转左150右80，本周 L=150+200=350 R=80+300=380
    # 配对350，奖金3500分，右侧结转30
    assert (l1.week_bonus, l1.paid, l1.debt) == (3500, 3500, 0)
    assert b2.line_for(2).week_bonus == 0
    assert b2.line_for(3).week_bonus == 0
    # 第3周无人下单：1号不活跃，右侧30继续结转（不配对）
    b3 = eng.settle()
    assert b3.line_for(1).week_bonus == 0
    # 第4周1号活跃：右侧带来结转30，左0右30，配对0
    eng.post_volume(1, 4, 100, "o6")
    b4 = eng.settle()
    assert b4.line_for(1).week_bonus == 0
    # 批次不可变：重读第2批次内容一致
    assert eng.get_run(2) == b2
    eng.close()
