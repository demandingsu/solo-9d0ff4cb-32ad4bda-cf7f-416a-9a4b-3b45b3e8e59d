"""
终端存档核对与流水段修复补传。

业务规则（对应车间存档找回流程）：

- 清单针对一个班次的一台终端，由若干互不重叠的流水号闭区间（段）组成，
  每段带存档 SHA-256 摘要。
- 段摘要：段内各笔按流水号升序，拼接 UTF-8 文本 ``流水号,耗时\\n``
  （耗时为整数毫秒），再做 SHA-256（小写十六进制）。
- 核对：用同一份本地明细逐段计算摘要并与存档比对，列出不一致区间，
  每个不一致段签发一个修复编号 repair_id，并记录核对时刻的段状态快照。
- 凭编号补传其中一段的完整明细：段内流水号不重复；先验补传内容自身摘要，
  摘要与存档一致才可整段替换；替换删除段内全部旧流水（含多出的旧号），
  段外流水原样保留。
- 补传摘要验过后：若库里这段已与存档相符，返回"已补齐"，不改库；
  若仍不符且相对核对时刻已有变动，则退回（需重新核对）。
- 段外有补录仍可修复。
- 按生效时的班报定档：替换后校验该终端各档笔数不得超过班报声明，
  且每笔耗时落在所属分档闭区间内。
- 普通补交与修复交错时，后写一方在先写入的结果上校验，保留本次范围外流水。
- 校验失败整次不改库（全部操作在单事务内完成）。
"""
from __future__ import annotations

import hashlib
import uuid

from . import db as db_mod
from .db import Database


class ArchiveError(Exception):
    """存档核对/修复业务错误，errors 为可直接返回给调用方的结构。"""

    def __init__(self, errors: list[dict] | str, status_code: int = 422):
        if isinstance(errors, str):
            errors = [{"error": errors}]
        self.errors = errors
        self.status_code = status_code
        super().__init__(str(errors))


# ---------------------------------------------------------------- 摘要

def segment_digest(items: list[dict]) -> str:
    """
    按流水号升序拼接 ``流水号,耗时\\n`` 的 UTF-8 文本并计算 SHA-256。

    入参 items 为同一终端、同一段内的明细 dict（含 seq、value_ms）。
    """
    ordered = sorted(items, key=lambda r: r["seq"])
    buf = "".join(f"{r['seq']},{r['value_ms']}\n" for r in ordered)
    return hashlib.sha256(buf.encode("utf-8")).hexdigest()


def _is_hex256(s: str) -> bool:
    if not isinstance(s, str) or len(s) != 64:
        return False
    try:
        int(s, 16)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------- 清单校验

def validate_manifest(segments: list[dict]) -> None:
    errors: list[dict] = []
    if not segments:
        raise ArchiveError("清单至少需要一个流水段")
    for i, seg in enumerate(segments):
        lo, hi = seg["seq_lo"], seg["seq_hi"]
        if not isinstance(lo, int) or not isinstance(hi, int) or lo < 1:
            errors.append({"index": i, "error": "流水号必须为不小于 1 的整数"})
        elif lo > hi:
            errors.append({"index": i,
                           "error": f"下界 {lo} 大于上界 {hi}"})
        if not _is_hex256(seg.get("sha256", "")):
            errors.append({"index": i,
                           "error": "sha256 必须为 64 位十六进制字符串"})
    # 互不重叠闭区间：按 lo 排序后，相邻段必须 lo > 前一段 hi
    ordered = sorted(
        [(s["seq_lo"], s["seq_hi"], i) for i, s in enumerate(segments)
         if isinstance(s["seq_lo"], int) and isinstance(s["seq_hi"], int)
         and s["seq_lo"] <= s["seq_hi"]],
        key=lambda t: t[0])
    for k in range(1, len(ordered)):
        prev_lo, prev_hi, prev_i = ordered[k - 1]
        lo, hi, i = ordered[k]
        if lo <= prev_hi:
            errors.append({"index": i,
                           "error": f"段 [{lo},{hi}] 与段 {prev_i} "
                                    f"[{prev_lo},{prev_hi}] 重叠"})
    if errors:
        raise ArchiveError(errors)


# ---------------------------------------------------------------- 核对

def reconcile(db: Database, shift_id: str, terminal_id: str,
              segments: list[dict]) -> dict:
    """
    用本地明细核对存档清单，返回每段核对结果，并为不一致段签发修复作业。
    """
    report = db.get_report(shift_id)
    if report is None:
        raise ArchiveError(f"班次 {shift_id} 的班报不存在，请先登记班报", 404)
    declared_terms = {t["terminal_id"] for t in report["terminals"]}
    if terminal_id not in declared_terms:
        raise ArchiveError(
            f"终端 {terminal_id} 未在班次 {shift_id} 的班报中声明")

    validate_manifest(segments)

    local = db.list_details(shift_id, terminal_id)
    by_seq = {r["seq"]: r for r in local}

    results = []
    new_jobs = []
    mismatch_count = 0
    for seg in segments:
        lo, hi, want = seg["seq_lo"], seg["seq_hi"], seg["sha256"].lower()
        in_range = [by_seq[s] for s in range(lo, hi + 1) if s in by_seq]
        actual = segment_digest(in_range)
        present = sorted(r["seq"] for r in in_range)
        missing = [s for s in range(lo, hi + 1) if s not in by_seq]
        match = actual == want
        row = {
            "segment_no": seg.get("segment_no"),
            "seq_lo": lo,
            "seq_hi": hi,
            "expected_sha256": want,
            "actual_sha256": actual,
            "match": match,
            "local_count": len(in_range),
            "present_seqs": present,
            "missing_seqs": missing,
        }
        if not match:
            mismatch_count += 1
            repair_id = uuid.uuid4().hex
            row["repair_id"] = repair_id
            new_jobs.append({
                "repair_id": repair_id,
                "segment_no": seg.get("segment_no"),
                "seq_lo": lo,
                "seq_hi": hi,
                "expected_digest": want,
                "snapshot_digest": actual,   # 核对时刻的库内段状态
            })
        results.append(row)

    if new_jobs:
        db.create_repair_jobs(shift_id, terminal_id, new_jobs)

    return {
        "shift_id": shift_id,
        "terminal_id": terminal_id,
        "segment_count": len(segments),
        "mismatch_count": mismatch_count,
        "all_match": mismatch_count == 0,
        "segments": results,
    }


# ---------------------------------------------------------------- 修复补传

def _bucket_of_value(report: dict, value_ms: int) -> int:
    """按生效班报的分档闭区间为耗时定档，返回 bucket_idx；无法定档抛错。"""
    for i, b in enumerate(report["buckets"]):
        if b["lo"] <= value_ms <= b["hi"]:
            return i
    raise ArchiveError(
        f"耗时 {value_ms}ms 不落在班报任何分档闭区间内，无法定档")


def _validate_terminal_capacities(report: dict, terminal_id: str,
                                  terminal_rows: list[dict]) -> None:
    """按生效班报校验该终端各档笔数与耗时。"""
    capacities = {t["bucket_idx"]: t["count"]
                  for t in report["terminals"]
                  if t["terminal_id"] == terminal_id}
    used: dict[int, int] = {}
    errors: list[dict] = []
    for r in terminal_rows:
        bidx = r["bucket_idx"]
        if bidx not in capacities:
            errors.append({"seq": r["seq"],
                           "error": f"班报未声明终端 {terminal_id} "
                                    f"在分档 {bidx} 的笔数"})
            continue
        b = report["buckets"][bidx]
        if not (b["lo"] <= r["value_ms"] <= b["hi"]):
            errors.append({"seq": r["seq"],
                           "error": f"耗时 {r['value_ms']}ms 超出分档 "
                                    f"{bidx} 闭区间 [{b['lo']},{b['hi']}]"})
        used[bidx] = used.get(bidx, 0) + 1
    for bidx, cnt in used.items():
        if bidx in capacities and cnt > capacities[bidx]:
            errors.append({"bucket_idx": bidx, "stored": cnt,
                           "capacity": capacities[bidx],
                           "error": "替换后该终端档内笔数超过班报声明"})
    if errors:
        raise ArchiveError(errors)


def complete_repair(db: Database, repair_id: str,
                    items: list[dict]) -> dict:
    """凭修复编号补传一段完整明细，在单事务内完成校验与整段替换。"""
    # ---- 0) 作业存在性与状态（单独读取，给未知编号返回 404）
    job = db.get_repair_job(repair_id)
    if job is None:
        raise ArchiveError(f"修复编号 {repair_id} 不存在", 404)
    if job["status"] == "superseded":
        raise ArchiveError(
            "该修复作业已被后来的核对取代，请重新核对获取新修复编号", 409)
    if job["status"] == "completed":
        # 已完成作业：库内段仍与存档相符则确认"已补齐"，否则要求重新核对
        local = db.list_details(job["shift_id"], job["terminal_id"])
        cur = [r for r in local
               if job["seq_lo"] <= r["seq"] <= job["seq_hi"]]
        if segment_digest(cur) == job["expected_digest"]:
            return {"repair_id": repair_id, "outcome": "already_complete",
                    "replaced": False, "segment_count": len(cur)}
        raise ArchiveError(
            "该修复作业已完成但库内段当前与存档不符，请重新核对", 409)

    lo, hi = job["seq_lo"], job["seq_hi"]

    if not items:
        raise ArchiveError("补传明细不能为空")

    # ---- 1) 结构校验（不依赖库）：段内流水号不重复、且都落在该段闭区间内
    errors: list[dict] = []
    seen: set[int] = set()
    for idx, it in enumerate(items):
        seq = it["seq"]
        if not isinstance(seq, int) or seq < 1:
            errors.append({"index": idx, "error": "流水号必须为不小于 1 的整数"})
            continue
        if not (lo <= seq <= hi):
            errors.append({"index": idx, "seq": seq,
                           "error": f"流水号 {seq} 不在本段 [{lo},{hi}] 内"})
        if seq in seen:
            errors.append({"index": idx, "seq": seq,
                           "error": "段内流水号重复"})
        seen.add(seq)
    if errors:
        raise ArchiveError(errors)

    ordered_items = sorted(items, key=lambda r: r["seq"])

    # ---- 2) 以下 读生效班报 -> 定档/摘要 -> 快照判定 -> 容量校验 -> 整段替换
    #         全部在同一个数据库事务（单次持锁）内完成；validator 抛错即回滚，
    #         与普通补交并发时双方都基于对方已提交的最新状态决策，杜绝写偏。
    def validator(conn, job_now, report, local):
        if report is None:
            raise ArchiveError(
                f"班次 {job_now['shift_id']} 的班报不存在", 404)
        # 事务内复核作业状态（防止进入事务前被并发核对置为 superseded）
        if job_now["status"] == "superseded":
            raise ArchiveError(
                "该修复作业已被后来的核对取代，请重新核对获取新修复编号", 409)

        # 2a) 按事务内生效班报为每笔定档，并独立复算补传内容摘要
        incoming: list[dict] = []
        for it in ordered_items:
            bidx = _bucket_of_value(report, it["value_ms"])
            incoming.append({"seq": it["seq"], "bucket_idx": bidx,
                             "value_ms": it["value_ms"]})
        upload_digest = segment_digest(incoming)
        want = job_now["expected_digest"]
        if upload_digest != want:
            raise ArchiveError([
                {"error": "补传内容摘要与存档不一致，拒绝整段替换",
                 "expected_sha256": want,
                 "upload_sha256": upload_digest}])

        # 2b) 库内段现状判定（基于事务内最新明细）
        current_seg = [r for r in local
                       if job_now["seq_lo"] <= r["seq"] <= job_now["seq_hi"]]
        current_digest = segment_digest(current_seg)

        # 库里这段已与存档相符：已补齐，仅置作业完成，明细不动
        if current_digest == want:
            payload = {
                "repair_id": repair_id,
                "outcome": "already_complete",
                "replaced": False,
                "segment_count": len(current_seg),
                "current_sha256": current_digest,
            }
            return False, payload, None

        # 仍不符且相对核对时刻快照已变动：退回（需重新核对）
        if current_digest != job_now["snapshot_digest"]:
            raise ArchiveError([
                {"error": "库内该段自核对后已发生变动且仍与存档不符，"
                          "本次补传退回，请重新核对后再修复",
                 "snapshot_sha256": job_now["snapshot_digest"],
                 "current_sha256": current_digest}], 409)

        # 2c) 整段替换前，按生效班报校验替换后的完整终端状态
        #     段外流水（含段外补录）原样保留，仅段内被补传内容替换
        outside = [r for r in local
                   if not (job_now["seq_lo"] <= r["seq"] <= job_now["seq_hi"])]
        merged_terminal = outside + incoming
        _validate_terminal_capacities(
            report, job_now["terminal_id"], merged_terminal)

        # 2d) 同事务删除段内旧号（含多出的）并插入补传
        bucket_counts: dict[int, int] = {}
        for r in merged_terminal:
            bucket_counts[r["bucket_idx"]] = \
                bucket_counts.get(r["bucket_idx"], 0) + 1
        payload = {
            "repair_id": repair_id,
            "outcome": "repaired",
            "replaced": True,
            "shift_id": job_now["shift_id"],
            "terminal_id": job_now["terminal_id"],
            "segment_no": job_now["segment_no"],
            "seq_lo": job_now["seq_lo"],
            "seq_hi": job_now["seq_hi"],
            "segment_count": len(incoming),
            "deleted_and_replaced": len(current_seg),
            "outside_preserved": len(outside),
            "stored_sha256": want,
            "terminal_bucket_counts": [
                {"bucket_idx": b, "count": c}
                for b, c in sorted(bucket_counts.items())],
        }
        return True, payload, incoming

    try:
        return db.run_repair_tx(repair_id, validator)
    except db_mod._RepairNotFound:
        raise ArchiveError(f"修复编号 {repair_id} 不存在", 404)
