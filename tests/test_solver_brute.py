"""小规模暴力枚举独立校验求解器：枚举每一种读数组合，逐笔排序核对。"""
from __future__ import annotations

import itertools
from fractions import Fraction

import pytest

from app.solver import (BucketSpec, Entry, build_quantiles,
                        check_convention, position_k)


def brute_force(entries, bucket_specs, quantiles):
    """枚举自由读数的全部组合，返回可行总耗时集合。"""
    domains = []
    for e in entries:
        lo = bucket_specs[e.bucket_idx].lo
        hi = bucket_specs[e.bucket_idx].hi
        fixed = []
        for v, c in e.pins.items():
            fixed.extend([v] * c)
        free = e.count - len(fixed)
        domains.append((fixed, list(range(lo, hi + 1)), free))

    choice_lists = [list(itertools.product(dom, repeat=free))
                    for (_fixed, dom, free) in domains]
    feasible_sums = set()
    for choices in itertools.product(*choice_lists):
        all_vals, total = [], 0
        for fixed, picks in zip([d[0] for d in domains], choices):
            all_vals.extend(fixed)
            all_vals.extend(picks)
            total += sum(fixed) + sum(picks)
        sv = sorted(all_vals)
        if all(sv[qr.k - 1] == qr.q for qr in quantiles):
            feasible_sums.add(total)
    return feasible_sums


def make_case(seed):
    """手工构造若干互不重叠分档与小规模条目（含固定明细）。"""
    specs = [BucketSpec(0, 2), BucketSpec(5, 7), BucketSpec(10, 12)]
    cases = [
        [Entry("T1", 0, 3, {1: 1}),
         Entry("T2", 1, 2, {}),
         Entry("T3", 2, 2, {11: 1})],
        [Entry("T1", 0, 2, {}),
         Entry("T2", 0, 1, {2: 1}),
         Entry("T3", 1, 3, {6: 2}),
         Entry("T4", 2, 1, {})],
        [Entry("T1", 1, 4, {5: 1, 7: 1}),
         Entry("T2", 2, 3, {})],
        [Entry("T1", 0, 1, {}),
         Entry("T2", 2, 2, {12: 2})],
    ]
    return specs, cases[seed % len(cases)]


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("rule", ["ceil", "floor"])
def test_solver_matches_brute_force(seed, rule):
    specs, entries = make_case(seed)
    n = sum(e.count for e in entries)
    # 任选分位数值：在可能取值集合里挑两个
    p50, p95 = (1, 10) if seed % 2 == 0 else (2, 7)
    quantiles = build_quantiles(p50, p95, n, rule)
    feasible_sums = brute_force(entries, specs, quantiles)

    lo_sum = sum(specs[e.bucket_idx].lo * e.count for e in entries)
    hi_possible = sum(specs[e.bucket_idx].hi * e.count for e in entries)
    tested = set(range(lo_sum, hi_possible + 1))

    for s in tested:
        rep = check_convention(specs, entries, p50, p95, s, rule)
        assert rep.feasible == (s in feasible_sums), (
            f"seed={seed} rule={rule} sum={s} "
            f"solver={rep.feasible} brute={s in feasible_sums} "
            f"reason={rep.reason}")
        if rep.feasible:
            w = rep.witness
            # 见证必须可独立复算
            dist = {r["value_ms"]: r["count"] for r in w["distribution"]}
            assert sum(dist.values()) == n
            assert sum(v * c for v, c in dist.items()) == s
            for e in entries:
                for v, c in e.pins.items():
                    assert dist[v] >= c          # 固定明细不能被改动
            cum = w["cumulative"]
            for qr in quantiles:
                got = next(r["value_ms"] for r in cum
                           if qr.k <= r["cumulative_count"])
                assert got == qr.q


def test_position_rules():
    assert position_k(Fraction(1, 2), 11, "ceil") == 6
    assert position_k(Fraction(1, 2), 11, "floor") == 5
    assert position_k(Fraction(19, 20), 11, "ceil") == 11
    assert position_k(Fraction(19, 20), 11, "floor") == 10
    # 至少第 1 项
    assert position_k(Fraction(1, 2), 1, "floor") == 1
    assert position_k(Fraction(1, 2), 1, "ceil") == 1
    # 整除时两口径一致
    assert position_k(Fraction(1, 2), 12, "ceil") == 6
    assert position_k(Fraction(1, 2), 12, "floor") == 6


def test_huge_counts_no_expansion():
    """分档笔数 10^12：求解与见证规模只与分档/阈值数有关，与总笔数无关。"""
    specs = [BucketSpec(0, 10), BucketSpec(11, 20), BucketSpec(21, 30)]
    entries = [Entry("T1", 0, 6 * 10**12),
               Entry("T2", 1, 4 * 10**12),
               Entry("T3", 2, 2 * 10**12)]
    n = 12 * 10**12
    rep_ceil = check_convention(specs, entries, 10, 21, 150 * 10**12, "ceil")
    rep_floor = check_convention(specs, entries, 10, 21, 150 * 10**12, "floor")
    assert rep_ceil.feasible and rep_floor.feasible
    for rep in (rep_ceil, rep_floor):
        w = rep.witness
        assert w["total_count"] == n
        assert w["total_sum_ms"] == 150 * 10**12
        assert len(w["distribution"]) <= len(entries) * 4
        for qr in build_quantiles(10, 21, n, rep.rule):
            got = next(r["value_ms"] for r in w["cumulative"]
                       if qr.k <= r["cumulative_count"])
            assert got == qr.q
