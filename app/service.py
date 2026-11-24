"""业务校验与求解编排：班报校验、明细提交/更正退回、双口径核对。"""
from __future__ import annotations

from .db import Database
from .solver import BucketSpec, Entry, check_convention


class ValidationError(Exception):
    def __init__(self, errors: list[dict] | str):
        if isinstance(errors, str):
            errors = [{"error": errors}]
        self.errors = errors
        super().__init__(str(errors))


# ---------------------------------------------------------------- 班报

def validate_report(payload: dict) -> None:
    errors: list[dict] = []
    buckets = payload["buckets"]
    if not buckets:
        errors.append({"field": "buckets", "error": "至少需要一个分档"})
    for i, b in enumerate(buckets):
        if b["lo"] > b["hi"]:
            errors.append({"field": f"buckets[{i}]",
                           "error": f"下界 {b['lo']} 大于上界 {b['hi']}"})
    # 共用分档：互不重叠的整数毫秒闭区间
    for i in range(len(buckets)):
        for j in range(i + 1, len(buckets)):
            a, b = buckets[i], buckets[j]
            if a["lo"] <= b["hi"] and b["lo"] <= a["hi"]:
                errors.append({"field": "buckets",
                               "error": f"分档 {i} 与 {j} 区间重叠"})
    if payload["p50_ms"] > payload["p95_ms"]:
        errors.append({"field": "p50_ms",
                       "error": "P50 不应大于 P95"})
    if payload["total_sum_ms"] < 0:
        errors.append({"field": "total_sum_ms", "error": "总耗时不能为负"})

    pairs: set[tuple[str, int]] = set()
    declared: set[tuple[str, int]] = set()
    for i, t in enumerate(payload["terminals"]):
        if t["bucket_idx"] < 0 or t["bucket_idx"] >= len(buckets):
            errors.append({"field": f"terminals[{i}].bucket_idx",
                           "error": "分档序号越界"})
            continue
        if t["count"] < 0:
            errors.append({"field": f"terminals[{i}].count",
                           "error": "笔数不能为负"})
        key = (t["terminal_id"], t["bucket_idx"])
        if key in pairs:
            errors.append({"field": f"terminals[{i}]",
                           "error": "同一终端在同一分档重复声明"})
        pairs.add(key)
        declared.add(key)
    if errors:
        raise ValidationError(errors)


# ---------------------------------------------------------------- 明细

def submit_details(db: Database, shift_id: str, items: list[dict]) -> dict:
    report = db.get_report(shift_id)
    if report is None:
        raise ValidationError(f"班次 {shift_id} 的班报不存在，请先登记班报")

    bucket_specs = report["buckets"]
    capacity: dict[tuple[str, int], int] = {
        (t["terminal_id"], t["bucket_idx"]): t["count"]
        for t in report["terminals"]}

    errors: list[dict] = []
    merged: dict[tuple[str, int], tuple[int, int]] = {}
    for r in db.list_details(shift_id):
        merged[(r["terminal_id"], r["seq"])] = (r["bucket_idx"], r["value_ms"])

    corrected = 0
    inserted = 0
    for idx, it in enumerate(items):
        term, seq, bidx, val = (it["terminal_id"], it["seq"],
                                it["bucket_idx"], it["value_ms"])
        if bidx < 0 or bidx >= len(bucket_specs):
            errors.append({"index": idx, "terminal_id": term, "seq": seq,
                           "error": f"分档序号 {bidx} 越界"})
            continue
        b = bucket_specs[bidx]
        if not (b["lo"] <= val <= b["hi"]):
            errors.append({"index": idx, "terminal_id": term, "seq": seq,
                           "error": f"耗时 {val}ms 超出本终端分档闭区间 "
                                    f"[{b['lo']}, {b['hi']}]"})
            continue
        if (term, bidx) not in capacity:
            errors.append({"index": idx, "terminal_id": term, "seq": seq,
                           "error": "班报未声明该终端在此分档的笔数"})
            continue
        key = (term, seq)
        corrected += int(key in merged)
        inserted += int(key not in merged)
        merged[key] = (bidx, val)

    if errors:
        raise ValidationError(errors)          # 整批退回，不落库

    # 逐 (终端, 分档) 检查档内笔数上限
    used: dict[tuple[str, int], int] = {}
    for (term, _seq), (bidx, _val) in merged.items():
        used[(term, bidx)] = used.get((term, bidx), 0) + 1
    over = [{"terminal_id": k[0], "bucket_idx": k[1],
             "stored": v, "capacity": capacity[k],
             "error": "已提交笔数超出本终端档内笔数"}
            for k, v in used.items() if v > capacity[k]]
    if over:
        raise ValidationError(over)

    return db.replace_merged(shift_id, merged,
                             {"corrected": corrected, "inserted": inserted})


# ---------------------------------------------------------------- 核对

def _build_solver_inputs(report: dict, details: list[dict]):
    bucket_specs = [BucketSpec(b["lo"], b["hi"]) for b in report["buckets"]]
    entries = [Entry(t["terminal_id"], t["bucket_idx"], t["count"])
               for t in report["terminals"]]
    idx_of = {(e.terminal_id, e.bucket_idx): i for i, e in enumerate(entries)}
    for d in details:
        i = idx_of[(d["terminal_id"], d["bucket_idx"])]
        pins = entries[i].pins
        pins[d["value_ms"]] = pins.get(d["value_ms"], 0) + 1
    return bucket_specs, entries


def run_check(db: Database, shift_id: str) -> dict:
    report = db.get_report(shift_id)
    if report is None:
        raise ValidationError(f"班次 {shift_id} 的班报不存在")
    details = db.list_details(shift_id)
    bucket_specs, entries = _build_solver_inputs(report, details)
    n = sum(e.count for e in entries)
    if n <= 0:
        raise ValidationError("班报总笔数为 0，无法计算分位数")

    common = {
        "shift_id": shift_id,
        "total_count": n,
        "pinned_count": sum(sum(e.pins.values()) for e in entries),
        "rules": [
            {"rule": "ceil", "formula": "第 ceil(pN) 项（至少第 1 项）"},
            {"rule": "floor", "formula": "第 floor(pN) 项（至少第 1 项）"},
        ],
    }
    reports = []
    for rule in ("ceil", "floor"):
        r = check_convention(bucket_specs, entries,
                             report["p50_ms"], report["p95_ms"],
                             report["total_sum_ms"], rule)
        reports.append({
            "rule": r.rule,
            "feasible": r.feasible,
            "reason": r.reason,
            "quantile_positions": r.quantiles,
            "feasible_sum_range_ms": (
                {"min": r.sum_bounds[0], "max": r.sum_bounds[1]}
                if r.sum_bounds else None),
            "witness": r.witness,
        })
    common["conventions"] = reports
    common["any_feasible"] = any(r["feasible"] for r in reports)
    return common
