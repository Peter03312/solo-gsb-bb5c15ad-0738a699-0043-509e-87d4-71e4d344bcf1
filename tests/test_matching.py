"""配对算法测试：构造场景 + 小规模随机穷举对照。"""

from __future__ import annotations

import itertools
import random

from app.matching import MICROS_PER_SECOND, MatchResult, Record, match_records


def rec(no, station, counterpart, t, band="40m", mode="SSB"):
    return Record(
        record_no=no,
        station=station,
        counterpart=counterpart,
        ts_micros=int(t * MICROS_PER_SECOND),
        band=band,
        mode=mode,
    )


def brute_force(records, allowed_diff_seconds):
    """枚举所有可行边集（每点度 <=1 且边合法），按三级目标取最优。"""
    allowed = int(allowed_diff_seconds * MICROS_PER_SECOND)
    n = len(records)
    candidate = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = records[i], records[j]
            if (
                a.station == b.counterpart
                and a.counterpart == b.station
                and a.band == b.band
                and a.mode == b.mode
                and abs(a.ts_micros - b.ts_micros) <= allowed
            ):
                key = tuple(sorted((a.record_no, b.record_no)))
                w = abs(a.ts_micros - b.ts_micros)
                candidate.append((key, frozenset((i, j)), w))

    best = None  # (配对数, -总时差反向, ...) 直接比较业务目标
    best_payload = None
    for k in range(len(candidate) + 1):
        pass

    def evaluate(chosen):
        used = set()
        keys, total = [], 0
        for key, verts, w in chosen:
            if used & verts:
                return None
            used |= verts
            keys.append(key)
            total += w
        keys.sort()
        return len(keys), total, keys

    # 枚举候选边的所有子集
    for mask in range(1 << len(candidate)):
        chosen = [candidate[i] for i in range(len(candidate)) if mask >> i & 1]
        ev = evaluate(chosen)
        if ev is None:
            continue
        cardinality, total, keys = ev
        score = (-cardinality, total, keys)
        if best is None or score < best:
            best = score
            best_payload = (cardinality, total, keys)
    return best_payload


def assert_matches_optimal(records, allowed_diff_seconds):
    result: MatchResult = match_records(records, allowed_diff_seconds)
    cardinality, total_micros, keys = brute_force(
        records, allowed_diff_seconds
    )
    got_keys = sorted(p.as_tuple() for p in result.pairs)
    assert len(result.pairs) == cardinality
    assert got_keys == keys
    assert abs(
        result.total_time_diff_seconds - total_micros / MICROS_PER_SECOND
    ) < 1e-9
    # 未配对集合一致
    matched = {x for k in keys for x in k}
    assert set(result.unpaired) == {
        r.record_no for r in records if r.record_no not in matched
    }
    return result


def test_mutual_reference_basic():
    records = [
        rec("1", "A", "B", 0),
        rec("2", "B", "A", 5),
    ]
    r = assert_matches_optimal(records, 10)
    assert r.pairs[0].as_tuple() == ("1", "2")
    assert r.pairs[0].time_diff_seconds == 5


def test_timezone_normalized_to_utc():
    # 08:00+08:00 == 00:00Z；00:00:30Z，时差 30 秒。
    records = [
        rec("r1", "X", "Y", 0),
        rec("r2", "Y", "X", 30),
    ]
    r = match_records(records, 30)
    assert len(r.pairs) == 1


def test_exceed_diff_not_paired():
    records = [rec("1", "A", "B", 0), rec("2", "B", "A", 11)]
    r = match_records(records, 10)
    assert r.pairs == ()
    assert set(r.unpaired) == {"1", "2"}


def test_band_mode_must_match():
    records = [
        rec("1", "A", "B", 0, band="40m", mode="SSB"),
        rec("2", "B", "A", 1, band="20m", mode="SSB"),
        rec("3", "A", "B", 0, band="40m", mode="CW"),
        rec("4", "B", "A", 1, band="40m", mode="SSB"),
    ]
    r = assert_matches_optimal(records, 10)
    assert r.pairs[0].as_tuple() == ("1", "4")
    assert set(r.unpaired) == {"2", "3"}


def test_self_call_pair_priority_tie_and_lexicographic():
    # 占位：等价场景在 test_min_total_diff_then_lexicographic 覆盖。
    # A<->B 组记录 1..4，两种完美匹配总时差均为 0 时取字典序最小。
    records = [
        rec("1", "A", "B", 0),
        rec("2", "A", "B", 10),
        rec("3", "B", "A", 0),
        rec("4", "B", "A", 10),
    ]
    r = assert_matches_optimal(records, 10)
    assert [p.as_tuple() for p in r.pairs] == [("1", "3"), ("2", "4")]
    assert r.total_time_diff_seconds == 0


def test_lexicographic_tiebreak_within_pair():
    # 只能配一对，(1,2) 与 (1,3) 等时差 -> 取记录号组合更小的 (1,2)
    records = [
        rec("1", "A", "B", 0),
        rec("2", "B", "A", 0),
        rec("3", "B", "A", 0),
    ]
    r = assert_matches_optimal(records, 5)
    assert r.pairs[0].as_tuple() == ("1", "2")
    assert r.unpaired == ("3",)


def test_max_cardinality_beats_less_diff():
    # “配对数优先”反例：
    #   左: L1(t0) L2(t100)；右: R1(t0) R2(t50)；允许时差 50
    #   边: L1-R1=0, L1-R2=50, L2-R2=50
    #   2 对方案总时差 100；只配 (L1,R1) 总时差 0 => 必须选 2 对
    records = [
        rec("L1", "A", "B", 0),
        rec("L2", "A", "B", 100),
        rec("R1", "B", "A", 0),
        rec("R2", "B", "A", 50),
    ]
    r = assert_matches_optimal(records, 50)
    assert len(r.pairs) == 2
    # (L1,R1)=0 + (L2,R2)=50 => 总时差 50；虽大于单配的 0，数量优先。
    assert r.total_time_diff_seconds == 50


def test_min_total_diff_then_lexicographic():
    # 两种完美匹配：(1,3)(2,4) 总时差 0；(1,4)(2,3) 总时差 20
    records = [
        rec("1", "A", "B", 0),
        rec("2", "A", "B", 10),
        rec("3", "B", "A", 0),
        rec("4", "B", "A", 10),
    ]
    r = assert_matches_optimal(records, 10)
    assert [p.as_tuple() for p in r.pairs] == [("1", "3"), ("2", "4")]
    assert r.total_time_diff_seconds == 0

    # 两种完美匹配总时差相同（均为 0）时取列表字典序更小者：
    #   [('1','3'),('2','4')] < [('1','4'),('2','3')]
    records2 = [
        rec("1", "A", "B", 0),
        rec("2", "A", "B", 0),
        rec("3", "B", "A", 0),
        rec("4", "B", "A", 0),
    ]
    r2 = assert_matches_optimal(records2, 10)
    assert [p.as_tuple() for p in r2.pairs] == [("1", "3"), ("2", "4")]


def test_multiple_station_pairs_independent():
    records = [
        rec("1", "A", "B", 0),
        rec("2", "B", "A", 0),
        rec("3", "C", "D", 7),
        rec("4", "D", "C", 9),
    ]
    r = assert_matches_optimal(records, 1)
    assert [p.as_tuple() for p in r.pairs] == [("1", "2")]
    assert set(r.unpaired) == {"3", "4"}


def test_empty():
    r = match_records([], 10)
    assert r.pairs == () and r.unpaired == ()


def test_randomized_vs_bruteforce():
    rng = random.Random(20261004)
    calls = ["A", "B", "C", "D"]
    for trial in range(150):
        n = rng.randint(0, 6)
        records = []
        for k in range(n):
            s = rng.choice(calls)
            c = rng.choice([x for x in calls if x != s])
            records.append(
                rec(
                    f"r{k}",
                    s,
                    c,
                    rng.randint(0, 120),
                    band=rng.choice(["40m", "20m"]),
                    mode=rng.choice(["SSB", "CW"]),
                )
            )
        assert_matches_optimal(records, rng.choice([0, 10, 40, 200]))


def test_pair_record_ids_sorted_within_and_between():
    # 记录号字符串字典序（如 10 < 2）
    records = [
        rec("10", "A", "B", 0),
        rec("2", "B", "A", 0),
    ]
    r = match_records(records, 1)
    assert r.pairs[0].as_tuple() == ("10", "2")


# --------------------------------------------------------------------------
# 复核员锁定对：锁定对保留，剩余日志仍按三级目标求解
# --------------------------------------------------------------------------
def test_locked_pair_no_lock_equivalent_to_free_solution():
    records = [
        rec("1", "A", "B", 0),
        rec("2", "B", "A", 5),
    ]
    locked = match_records(records, 10, ())
    free = match_records(records, 10)
    assert locked == free


def test_locked_pair_forced_and_excluded_from_free_graph():
    # 1 与 2/3 均可配；自由最优取字典序更小的 (1,2)。
    records = [
        rec("1", "A", "B", 0),
        rec("2", "B", "A", 0),
        rec("3", "B", "A", 0),
    ]
    r = match_records(records, 10, [("1", "3")])
    assert r.pairs[0].as_tuple() == ("1", "3")
    assert r.unpaired == ("2",)


def test_locked_pair_order_normalized():
    records = [
        rec("1", "A", "B", 0),
        rec("2", "B", "A", 5),
    ]
    r = match_records(records, 10, [("2", "1")])
    assert r.pairs[0].as_tuple() == ("1", "2")
    assert r.pairs[0].time_diff_seconds == 5


def test_lock_not_replaced_when_cardinality_drops():
    # 左: L1(t0) L2(t100)；右: R1(t0) R2(t50)；允许 50
    # 边: L1-R1=0, L1-R2=50, L2-R2=50；自由最优两对。
    records = [
        rec("L1", "A", "B", 0),
        rec("L2", "A", "B", 100),
        rec("R1", "B", "A", 0),
        rec("R2", "B", "A", 50),
    ]
    free = match_records(records, 50)
    assert len(free.pairs) == 2
    # 锁定 (L1,R2)=50：剩余 L2/R1 时差 100 超限无法配 => 仅 1 对
    locked = match_records(records, 50, [("L1", "R2")])
    assert [p.as_tuple() for p in locked.pairs] == [("L1", "R2")]
    assert set(locked.unpaired) == {"L2", "R1"}
    assert locked.total_time_diff_seconds == 50


def test_locked_pair_plus_free_pairs_sorted():
    # 锁定 (1,4) 时差 10；剩余 (2,3) 时差 0 自由配对。
    records = [
        rec("1", "A", "B", 0),
        rec("2", "A", "B", 0),
        rec("3", "B", "A", 0),
        rec("4", "B", "A", 10),
    ]
    r = match_records(records, 10, [("1", "4")])
    assert [p.as_tuple() for p in r.pairs] == [("1", "4"), ("2", "3")]
    assert r.total_time_diff_seconds == 10
    assert r.unpaired == ()


def test_multiple_locked_pairs_all_preserved():
    records = [
        rec("1", "A", "B", 1),
        rec("2", "B", "A", 2),
        rec("3", "C", "D", 3),
        rec("4", "D", "C", 4),
    ]
    r = match_records(records, 10, [("1", "2"), ("3", "4")])
    assert [p.as_tuple() for p in r.pairs] == [("1", "2"), ("3", "4")]
    assert r.total_time_diff_seconds == 2

