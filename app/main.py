"""FastAPI 入口：班报登记、终端明细提交/更正、双口径核对。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import archive, service
from .db import Database

DB_PATH = os.environ.get("APP_DB_PATH", "/data/shift.db")


class BucketIn(BaseModel):
    lo: int = Field(..., description="闭区间下界（整数毫秒）")
    hi: int = Field(..., description="闭区间上界（整数毫秒）")


class TerminalBucketIn(BaseModel):
    terminal_id: str
    bucket_idx: int = Field(..., ge=0, description="分档序号（buckets 下标）")
    count: int = Field(..., ge=0, description="该终端在该分档的笔数")


class ShiftReportIn(BaseModel):
    buckets: list[BucketIn] = Field(..., description="各终端共用、互不重叠的整数毫秒闭区间")
    terminals: list[TerminalBucketIn]
    p50_ms: int
    p95_ms: int
    total_sum_ms: int = Field(..., description="班报总耗时（全部读数之和，毫秒）")


class DetailItemIn(BaseModel):
    terminal_id: str
    seq: int = Field(..., ge=1, description="终端内流水号，同号再提交即为更正")
    bucket_idx: int
    value_ms: int


class DetailSubmitIn(BaseModel):
    items: list[DetailItemIn]


class ArchiveSegmentIn(BaseModel):
    segment_no: str | int | None = Field(
        None, description="段编号（车间存档段标识，仅作追踪用）")
    seq_lo: int = Field(..., ge=1, description="流水号闭区间下界")
    seq_hi: int = Field(..., ge=1, description="流水号闭区间上界")
    sha256: str = Field(..., description="该段存档 SHA-256（64 位十六进制）")


class ReconcileIn(BaseModel):
    segments: list[ArchiveSegmentIn] = Field(
        ..., description="一台终端一个班次的互不重叠流水号闭区间清单及各段摘要")


class RepairItemIn(BaseModel):
    seq: int = Field(..., ge=1, description="段内流水号，同段不可重复")
    value_ms: int = Field(..., description="该笔耗时（整数毫秒），按生效班报自动定档")


class RepairCompleteIn(BaseModel):
    items: list[RepairItemIn] = Field(
        ..., description="该段完整明细，按流水号升序拼接 流水号,耗时\\n 后摘要须与存档一致")


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    app.state.db = Database(DB_PATH)
    yield
    app.state.db.close()


app = FastAPI(
    title="扫码班报分位口径核对 / 终端存档修复补传",
    version="1.1.0",
    description=(
        "## 能力一：班报分位口径核对\n\n"
        "各终端只上报分档笔数，本服务用整数容量约束（不逐笔展开）核对班报 "
        "P50/P95/总耗时分别能否由 **同一组读数** 在两种取位口径下复算：\n\n"
        "- `ceil` 口径：排序后第 ⌈pN⌉ 项\n"
        "- `floor` 口径：排序后第 ⌊pN⌋ 项（均至少取第 1 项）\n\n"
        "可行时返回可独立复算的「耗时→笔数」见证分布。\n\n"
        "## 能力二：终端存档核对与流水段修复补传\n\n"
        "- 段摘要：段内按流水号升序拼接 UTF-8 文本 `流水号,耗时\\n` 后取 SHA-256；\n"
        "- `/reconcile` 用本地明细核对互不重叠流水段清单，列出不一致区间并签发 "
        "`repair_id`（记录核对时刻快照）；\n"
        "- `/repairs/{repair_id}/complete` 先验补传摘要，再按快照判定 "
        "已补齐/变动退回/整段替换；整段替换删除段内旧号、保留段外流水，"
        "并按生效班报定档校验各档笔数；校验失败整次不改库。"
    ),
    lifespan=lifespan,
)


def _db() -> Database:
    return app.state.db


@app.put("/shifts/{shift_id}/report",
         summary="登记/覆盖一班的分档笔数与班报分位数、总耗时")
def put_report(shift_id: str, body: ShiftReportIn):
    payload = body.model_dump()
    try:
        service.validate_report(payload)
        _db().upsert_report(shift_id, payload)
    except service.ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors)
    if _db().has_details(shift_id):
        return {"shift_id": shift_id, "replaced": True,
                "warning": "已有明细将按新班报重新校验，建议重新核对"}
    return {"shift_id": shift_id, "replaced": True}


@app.post("/shifts/{shift_id}/details",
          summary="提交/更正终端明细（同终端同流水号即更正；整批校验失败则退回）")
def post_details(shift_id: str, body: DetailSubmitIn):
    if not body.items:
        raise HTTPException(status_code=422,
                            detail=[{"error": "items 不能为空"}])
    try:
        result = service.submit_details(
            _db(), shift_id, [it.model_dump() for it in body.items])
    except service.ValidationError as exc:
        raise HTTPException(status_code=422,
                            detail={"rejected": True, "errors": exc.errors})
    return {"shift_id": shift_id, "rejected": False, **result}


@app.get("/shifts/{shift_id}/details", summary="查询某班已提交明细")
def get_details(shift_id: str):
    if _db().get_report(shift_id) is None:
        raise HTTPException(404, f"班次 {shift_id} 不存在")
    return {"shift_id": shift_id, "items": _db().list_details(shift_id)}


@app.post("/shifts/{shift_id}/check",
          summary="用同一份已提交明细，分别核对两种取位口径是否可行")
def post_check(shift_id: str):
    try:
        return service.run_check(_db(), shift_id)
    except service.ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors)


@app.post("/shifts/{shift_id}/terminals/{terminal_id}/reconcile",
          summary="用本地明细核对终端存档清单，列出不一致区间并签发修复编号")
def post_reconcile(shift_id: str, terminal_id: str, body: ReconcileIn):
    segments = []
    for s in body.segments:
        d = s.model_dump()
        if d.get("segment_no") is not None:
            d["segment_no"] = str(d["segment_no"])
        segments.append(d)
    try:
        return archive.reconcile(_db(), shift_id, terminal_id, segments)
    except archive.ArchiveError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.errors)


@app.get("/shifts/{shift_id}/repairs",
         summary="查询某班（可限定终端）的存档修复作业状态")
def get_repairs(shift_id: str, terminal_id: str | None = None):
    if _db().get_report(shift_id) is None:
        raise HTTPException(404, f"班次 {shift_id} 不存在")
    return {"shift_id": shift_id,
            "jobs": _db().list_repair_jobs(shift_id, terminal_id)}


@app.post("/repairs/{repair_id}/complete",
          summary="凭修复编号补传一段完整明细：验摘要、整段替换、按班报定档校验")
def post_repair_complete(repair_id: str, body: RepairCompleteIn):
    try:
        result = archive.complete_repair(
            _db(), repair_id, [it.model_dump() for it in body.items])
    except archive.ArchiveError as exc:
        raise HTTPException(status_code=exc.status_code,
                            detail={"rejected": True, "errors": exc.errors})
    return {"rejected": False, **result}


@app.get("/health", summary="健康检查")
def health():
    return {"status": "ok"}
