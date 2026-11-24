"""
分档口径核对求解器（纯整数运算，不按总笔数逐笔展开）。

输入：
  - 一组共用的整数毫秒闭区间分档 [lo, hi]（互不重叠）
  - 若干 (终端, 分档) 条目，每条给出档内笔数 count 与已提交明细 pins（值 -> 笔数）
  - 班报分位数 (q, k)：要求排序后第 k 项等于 q
  - 班报总耗时 S

判定：是否存在一组读数，同时满足
  1) 每个读数落在所属分档区间内（已提交明细的值固定）
  2) 对每个分位数，第 k 项恰好等于 q
  3) 全部读数之和等于 S

分位数条件等价于双侧容量约束（N 为总笔数）：
  小于 q 的笔数 <= k - 1，且 大于 q 的笔数 <= N - k

算法：
  最小和：自由读数全部取 lo，再按最小代价把读数上提到阈值，修复 below 容量，
          修复后复验 above 容量（lo 基线是 above 最宽松状态）；
  最大和：x=-v 取负后复用同一过程；
  可行和为二者之间的全部连续整数（整数容量流多面体的连通性），
  命中 S 时在最小和基线上用带容量余量的批量上提构造见证分布。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional


# ---------------------------------------------------------------- 数据结构

@dataclass(frozen=True)
class BucketSpec:
    lo: int
    hi: int


@dataclass
class Entry:
    """一个 (终端, 分档) 条目：count 个读数，pins 为已提交明细 值->笔数。"""
    terminal_id: str
    bucket_idx: int
    count: int
    pins: dict[int, int] = field(default_factory=dict)


@dataclass
class Group:
    """同一 entry 内当前取值均为 v 的一批自由读数（批量表示，不逐笔展开）。"""
    entry_idx: int
    lo: int
    hi: int
    v: int
    m: int


@dataclass
class QuantileReq:
    name: str
    p: Fraction
    q: int          # 班报分位数值
    k: int          # 该口径下要求的位置


# ---------------------------------------------------------------- 取位口径

def position_k(p: Fraction, n: int, rule: str) -> int:
    """第 ceil(pN) 项 / 第 floor(pN) 项，至少取第 1 项。"""
    pn = p * n
    num, den = pn.numerator, pn.denominator
    if rule == "ceil":
        raw = -(-num // den)          # ceil
    elif rule == "floor":
        raw = num // den
    else:
        raise ValueError(f"unknown rule: {rule}")
    return max(1, raw)


def build_quantiles(p50: int, p95: int, n: int, rule: str) -> list[QuantileReq]:
    specs = [("P50", Fraction(1, 2), p50), ("P95", Fraction(19, 20), p95)]
    return [QuantileReq(name, p, q, position_k(p, n, rule))
            for name, p, q in specs]


# ---------------------------------------------------------------- 极端和

@dataclass
class ExtremeResult:
    ok: bool
    reason: Optional[str]
    groups: list[Group]            # 自由读数的分组
    pin_sum: int
    free_sum: int


def _extreme_min(entries: list[Entry],
                 bounds: list[tuple[int, int]],
                 pins_x: list[dict[int, int]],
                 thresholds: list[int],
                 cap_below: dict[int, int],
                 cap_above: dict[int, int]) -> ExtremeResult:
    """
    x 空间内的最小和。
    - 先校验固定读数对 below / above 两类容量是否越界；
    - 自由读数全部取 lo，再按最小提价贪心逐个修复 below 容量（阈值升序）；
    - 修复后复验 above 容量（lo 基线是 above 最宽松状态，提价只会让它更紧）。
    """
    pin_sum = 0
    below_pin: dict[int, int] = {t: 0 for t in thresholds}
    above_pin: dict[int, int] = {t: 0 for t in thresholds}
    for pind in pins_x:
        for val, c in pind.items():
            pin_sum += val * c
            for t in thresholds:
                if val < t:
                    below_pin[t] += c
                elif val > t:
                    above_pin[t] += c
    for t in thresholds:
        if below_pin[t] > cap_below[t]:
            return ExtremeResult(
                False,
                f"已提交明细中小于 {t} 的笔数 {below_pin[t]} "
                f"超过容量 {cap_below[t]}", [], 0, 0)
        if above_pin[t] > cap_above[t]:
            return ExtremeResult(
                False,
                f"已提交明细中大于 {t} 的笔数 {above_pin[t]} "
                f"超过容量 {cap_above[t]}", [], 0, 0)
    eff_cap = {t: cap_below[t] - below_pin[t] for t in thresholds}

    groups: list[Group] = []
    free_sum = 0
    for i, e in enumerate(entries):
        lo, hi = bounds[i]
        m = e.count - sum(e.pins.values())
        if m < 0:
            return ExtremeResult(False, "明细笔数超过档内笔数", [], 0, 0)
        if m > 0:
            groups.append(Group(i, lo, hi, lo, m))
            free_sum += lo * m

    for t in thresholds:                     # 嵌套容量，从小到大修
        while True:
            below = sum(g.m for g in groups if g.v < t)
            if below <= eff_cap[t]:
                break
            need = below - eff_cap[t]
            cands = [g for g in groups if g.v < t <= g.hi]
            if not cands:
                return ExtremeResult(
                    False,
                    f"分位数约束不可满足：必须有至少 {need} 个读数达到 {t}，"
                    f"但所在分档上界不足", [], 0, 0)
            cands.sort(key=lambda g: -g.v)   # 提价 t-v 最小优先
            for g in cands:
                if need == 0:
                    break
                x = min(need, g.m)
                g.m -= x
                groups.append(Group(g.entry_idx, g.lo, g.hi, t, x))
                free_sum += x * (t - g.v)
                need -= x
            groups = [g for g in groups if g.m > 0]
            if need > 0:
                return ExtremeResult(
                    False,
                    f"分位数约束不可满足：{need} 个读数无法达到 {t}"
                    f"（分档上界不足）", [], 0, 0)

    # 复验 above 容量（固定 + 修复后的自由读数）
    for t in thresholds:
        above = above_pin[t] + sum(g.m for g in groups if g.v > t)
        if above > cap_above[t]:
            return ExtremeResult(
                False,
                f"分位数约束不可满足：大于 {t} 的读数至少 {above} 笔，"
                f"超过容量 {cap_above[t]}（分档下界不足）", [], 0, 0)
    return ExtremeResult(True, None, groups, pin_sum, free_sum)


def _extreme(entries: list[Entry],
             bucket_specs: list[BucketSpec],
             quantiles: list[QuantileReq],
             n: int,
             want_max: bool) -> ExtremeResult:
    """求自由读数的最小/最大可行和。max 通过 x=-v 取反转化为 min。"""
    # 合并同值阈值的容量（P50==P95 时取最紧）
    cap_below_v: dict[int, int] = {}
    cap_above_v: dict[int, int] = {}
    for qr in quantiles:
        cap_below_v[qr.q] = min(cap_below_v.get(qr.q, n), qr.k - 1)
        cap_above_v[qr.q] = min(cap_above_v.get(qr.q, n), n - qr.k)

    if not want_max:
        bounds = [(bucket_specs[e.bucket_idx].lo,
                   bucket_specs[e.bucket_idx].hi) for e in entries]
        pins_x = [dict(e.pins) for e in entries]
        thresholds = sorted(cap_below_v)
        return _extreme_min(entries, bounds, pins_x, thresholds,
                            cap_below_v, cap_above_v)

    # x = -v：区间、阈值取反；v 空间 above 容量变成 x 空间 below 容量，反之亦然
    bounds = [(-bucket_specs[e.bucket_idx].hi,
               -bucket_specs[e.bucket_idx].lo) for e in entries]
    pins_x = [{-v: c for v, c in e.pins.items()} for e in entries]
    cap_below_x = {-q: c for q, c in cap_above_v.items()}
    cap_above_x = {-q: c for q, c in cap_below_v.items()}
    thresholds = sorted(cap_below_x)
    res = _extreme_min(entries, bounds, pins_x, thresholds,
                       cap_below_x, cap_above_x)
    if not res.ok:
        # x 空间阈值是 -q，报错还原成 v 空间的 q，并交换比较方向
        for q in cap_below_v:
            res.reason = (res.reason or "").replace(str(-q), str(q))
        res.reason = (res.reason or "").replace("小于", "大于")
        return res
    for g in res.groups:
        g.lo, g.hi, g.v = -g.hi, -g.lo, -g.v
    res.pin_sum = -res.pin_sum
    res.free_sum = -res.free_sum
    return res


# ---------------------------------------------------------------- 见证构造

def _raise_witness(entries: list[Entry],
                   bucket_specs: list[BucketSpec],
                   quantiles: list[QuantileReq],
                   n: int,
                   min_groups: list[Group],
                   pin_sum: int,
                   free_min: int,
                   target_sum: int) -> Optional[tuple[list[Group], str]]:
    """在最小和基线上批量上提，使总和恰为 target_sum，全程守住 above 容量。"""
    groups = [Group(g.entry_idx, g.lo, g.hi, g.v, g.m) for g in min_groups]
    threshold_vals = sorted({qr.q for qr in quantiles})
    cap_above: dict[int, int] = {}
    for qr in quantiles:
        cap_above[qr.q] = min(cap_above.get(qr.q, n), n - qr.k)

    pins_by_entry = {i: dict(e.pins) for i, e in enumerate(entries)}

    def current_above(t: int) -> int:
        c = sum(cnt for pind in pins_by_entry.values()
                for val, cnt in pind.items() if val > t)
        c += sum(g.m for g in groups if g.v > t)
        return c

    allowance = {t: cap_above[t] - current_above(t) for t in threshold_vals}
    if any(a < 0 for a in allowance.values()):
        return None, "above 容量在最小和基线上即已超限"

    delta = target_sum - pin_sum - free_min
    if delta < 0:
        return None, "目标总耗时低于最小可行和"

    while delta > 0:
        progress = False
        # 先消耗不跨阈值的免费空间，再按余量跨阈值；每轮整组批量处理
        for g in sorted(groups, key=lambda x: x.v, reverse=True):
            if delta == 0:
                break
            if g.v >= g.hi:
                continue
            # 不跨任何阈值可到达的最高值
            next_t = next((t for t in threshold_vals if t >= g.v), None)
            free_ceiling = g.hi if next_t is None else min(g.hi, next_t)
            if free_ceiling > g.v:
                head = free_ceiling - g.v
                budget = min(delta, g.m * head)
                full, rem = divmod(budget, head)
                if full:
                    g.m -= full
                    groups.append(Group(g.entry_idx, g.lo, g.hi,
                                        g.v + head, full))
                    delta -= full * head
                    progress = True
                if rem and g.m > 0:
                    g.m -= 1
                    groups.append(Group(g.entry_idx, g.lo, g.hi,
                                        g.v + rem, 1))
                    delta -= rem
                    progress = True
                continue
            # g.v 落在某个阈值上：再上提 1 即跨阈值，需要 above 余量
            t = g.v
            if t not in allowance or allowance[t] <= 0:
                continue
            x = min(g.m, allowance[t], delta)
            if x == 0:
                continue
            g.m -= x
            groups.append(Group(g.entry_idx, g.lo, g.hi, t + 1, x))
            allowance[t] -= x
            delta -= x
            progress = True
        groups = [g for g in groups if g.m > 0]
        if not progress:
            return None, "无法在容量约束内达到目标总耗时"
    return groups, ""


# ---------------------------------------------------------------- 汇总复算

def _assemble(entries: list[Entry],
              bucket_specs: list[BucketSpec],
              groups: list[Group]) -> dict:
    """把自由分组与固定明细合并为可复算的 值->笔数 分布（全局 + 分终端）。"""
    per_entry: dict[int, dict[int, int]] = {i: {} for i in range(len(entries))}
    for i, e in enumerate(entries):
        for v, c in e.pins.items():
            per_entry[i][v] = per_entry[i].get(v, 0) + c
    for g in groups:
        d = per_entry[g.entry_idx]
        d[g.v] = d.get(g.v, 0) + g.m

    global_dist: dict[int, int] = {}
    by_terminal: dict[str, dict[int, int]] = {}
    term_sum: dict[str, int] = {}
    term_cnt: dict[str, int] = {}
    for i, e in enumerate(entries):
        term = e.terminal_id
        for v, c in per_entry[i].items():
            global_dist[v] = global_dist.get(v, 0) + c
            td = by_terminal.setdefault(term, {})
            td[v] = td.get(v, 0) + c
            term_sum[term] = term_sum.get(term, 0) + v * c
            term_cnt[term] = term_cnt.get(term, 0) + c

    distribution = [{"value_ms": v, "count": c}
                    for v, c in sorted(global_dist.items())]
    cumulative = []
    acc = 0
    for row in distribution:
        acc += row["count"]
        cumulative.append({"value_ms": row["value_ms"],
                           "count": row["count"],
                           "cumulative_count": acc})
    terminals = []
    for term in sorted(by_terminal):
        d = by_terminal[term]
        terminals.append({
            "terminal_id": term,
            "count": term_cnt[term],
            "sum_ms": term_sum[term],
            "distribution": [{"value_ms": v, "count": c}
                             for v, c in sorted(d.items())],
        })
    return {
        "distribution": distribution,
        "cumulative": cumulative,
        "by_terminal": terminals,
    }


def kth_value(cumulative: list[dict], k: int) -> Optional[int]:
    for row in cumulative:
        if k <= row["cumulative_count"]:
            return row["value_ms"]
    return None


# ---------------------------------------------------------------- 对外主入口

@dataclass
class ConventionReport:
    rule: str
    feasible: bool
    reason: Optional[str]
    quantiles: list[dict]
    witness: Optional[dict]
    sum_bounds: Optional[tuple[int, int]]


def check_convention(bucket_specs: list[BucketSpec],
                     entries: list[Entry],
                     p50: int,
                     p95: int,
                     total_sum: int,
                     rule: str) -> ConventionReport:
    n = sum(e.count for e in entries)
    if n <= 0:
        return ConventionReport(rule, False, "总笔数为 0", [], None, None)
    quantiles = build_quantiles(p50, p95, n, rule)
    qmeta = [{"quantile": qr.name,
              "p": f"{qr.p.numerator}/{qr.p.denominator}",
              "k": qr.k,
              "reported_value_ms": qr.q} for qr in quantiles]

    lo_res = _extreme(entries, bucket_specs, quantiles, n, want_max=False)
    if not lo_res.ok:
        return ConventionReport(rule, False, lo_res.reason, qmeta, None, None)
    hi_res = _extreme(entries, bucket_specs, quantiles, n, want_max=True)
    if not hi_res.ok:
        return ConventionReport(rule, False, hi_res.reason, qmeta, None, None)

    total_min = lo_res.pin_sum + lo_res.free_sum
    total_max = hi_res.pin_sum + hi_res.free_sum
    if not (total_min <= total_sum <= total_max):
        return ConventionReport(
            rule, False,
            f"总耗时 {total_sum} 不在可行区间 [{total_min}, {total_max}] 内"
            f"（同一组读数须同时复算两个分位数与总耗时）",
            qmeta, None, (total_min, total_max))

    built = _raise_witness(entries, bucket_specs, quantiles, n,
                           lo_res.groups, lo_res.pin_sum,
                           lo_res.free_sum, total_sum)
    if built is None:
        return ConventionReport(rule, False, "见证构造失败", qmeta, None,
                                (total_min, total_max))
    groups, err = built
    if err:
        return ConventionReport(rule, False, err, qmeta, None,
                                (total_min, total_max))

    assembled = _assemble(entries, bucket_specs, groups)

    # 独立复算：总和、笔数、两个分位数位置
    recomputed_sum = sum(r["value_ms"] * r["count"]
                         for r in assembled["distribution"])
    recomputed_n = sum(r["count"] for r in assembled["distribution"])
    positions = []
    for qr in quantiles:
        v = kth_value(assembled["cumulative"], qr.k)
        idx = next(i for i, row in enumerate(assembled["cumulative"])
                   if row["value_ms"] == v)
        before = 0 if idx == 0 else \
            assembled["cumulative"][idx - 1]["cumulative_count"]
        through = assembled["cumulative"][idx]["cumulative_count"]
        positions.append({"quantile": qr.name, "k": qr.k,
                          "value_ms": v,
                          "cumulative_before": before,
                          "cumulative_through": through})
    if recomputed_sum != total_sum or recomputed_n != n or \
            any(pos["value_ms"] != qr.q for pos, qr in
                zip(positions, quantiles)):
        return ConventionReport(rule, False,
                                "内部复算校验失败（实现缺陷）", qmeta, None,
                                (total_min, total_max))

    pinned_count = sum(sum(e.pins.values()) for e in entries)
    witness = {
        "total_count": n,
        "total_sum_ms": recomputed_sum,
        "pinned_count": pinned_count,
        "free_count": n - pinned_count,
        "positions": positions,
        **assembled,
    }
    return ConventionReport(rule, True, None, qmeta, witness,
                            (total_min, total_max))
