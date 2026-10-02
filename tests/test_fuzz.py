"""随机树 + 随机业绩（含大量迟到业绩与退货）对照参照实现。"""

import random

from bonus_engine import Engine

from .reference import Reference


def build_random_tree(rng, eng, ref, n_nodes):
    """随机安置 n_nodes 个分销商，返回 id 列表。"""
    ids = [1]
    eng.enroll(1)
    ref.enroll(1)
    # 每个节点的空闲侧
    free = {1: ["L", "R"]}
    next_id = 2
    while len(ids) < n_nodes:
        parent = rng.choice(list(free))
        side = rng.choice(free[parent])
        free[parent].remove(side)
        if not free[parent]:
            del free[parent]
        eng.enroll(next_id, parent, side)
        ref.enroll(next_id, parent, side)
        free[next_id] = ["L", "R"]
        ids.append(next_id)
        next_id += 1
    return ids


def run_fuzz(seed, tmp_path, n_nodes=300, n_weeks=30):
    rng = random.Random(seed)
    eng = Engine(str(tmp_path / f"fuzz{seed}.db"))
    ref = Reference()
    ids = build_random_tree(rng, eng, ref, n_nodes)

    orders: dict[str, list] = {}  # order_ref -> [dist, week, cumulative]
    order_seq = 0
    batches = []
    paid_sum = {d: 0 for d in ids}

    def post(dist, week, points):
        nonlocal order_seq
        order_seq += 1
        ref_tag = f"o{order_seq}"
        eng.post_volume(dist, week, points, ref_tag)
        ref.post_volume(dist, week, points)
        orders[ref_tag] = [dist, week, points]

    for week in range(1, n_weeks + 1):
        # 本周新业绩
        for dist in rng.sample(ids, k=rng.randint(5, 40)):
            post(dist, week, rng.choice([50, 100, 150, 300, 800, 2000]))
        # 迟到业绩：多数只晚一两周，个别回溯到第 1 周
        if week > 1:
            for _ in range(rng.randint(0, 25)):
                dist = rng.choice(ids)
                lag = rng.choice([1, 1, 1, 2, 2, 3, 5,
                                  rng.randint(1, week - 1)])
                late_week = max(1, week - lag)
                post(dist, late_week, rng.choice([80, 120, 500, 1500]))
            # 退货：记在原订单那一周（可能是已结算周）
            for _ in range(rng.randint(0, 8)):
                if not orders:
                    break
                ref_tag = rng.choice(list(orders))
                dist, orig_week, cum = orders[ref_tag]
                if cum <= 0:
                    continue
                amount = rng.randint(1, cum)
                eng.post_volume(dist, orig_week, -amount, ref_tag)
                ref.post_volume(dist, orig_week, -amount)
                orders[ref_tag][2] -= amount

        batch = eng.settle()
        expected = ref.settle()
        assert batch.week == week
        assert len(batch.lines) == len(ids)
        for line in batch.lines:
            wb, adj, paid, debt = expected[line.dist_id]
            assert (line.week_bonus, line.adjustment, line.paid, line.debt) \
                == (wb, adj, paid, debt), f"week={week} dist={line.dist_id}"
            assert line.paid >= 0
            assert line.debt >= 0
            if line.debt > 0:
                assert line.paid == 0
            paid_sum[line.dist_id] += line.paid
        # 硬性要求：历次实发之和 - 当前欠款 == 全量重放的累计奖金 S
        for d in ids:
            assert paid_sum[d] - batch.line_for(d).debt == ref.last_cum[d]
        # 所有旧批次逐字段未变
        for n, old in enumerate(batches, start=1):
            assert eng.get_run(n) == old
        batches.append(batch)

    eng.close()
    return batches


def test_fuzz_seed_1(tmp_path):
    run_fuzz(1, tmp_path)


def test_fuzz_seed_2(tmp_path):
    run_fuzz(2, tmp_path)


def test_fuzz_seed_3(tmp_path):
    run_fuzz(3, tmp_path, n_nodes=120, n_weeks=45)
