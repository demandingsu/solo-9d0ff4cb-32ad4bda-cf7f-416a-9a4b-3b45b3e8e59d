"""FastAPI 入口：班报登记、终端明细提交/更正、双口径核对。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import service
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    app.state.db = Database(DB_PATH)
    yield
    app.state.db.close()


app = FastAPI(
    title="扫码班报分位口径核对",
    version="1.0.0",
    description=(
        "各终端只上报分档笔数，本服务用整数容量约束（不逐笔展开）核对班报 "
        "P50/P95/总耗时分别能否由 **同一组读数** 在两种取位口径下复算：\n\n"
        "- `ceil` 口径：排序后第 ⌈pN⌉ 项\n"
        "- `floor` 口径：排序后第 ⌊pN⌋ 项（均至少取第 1 项）\n\n"
        "可行时返回可独立复算的「耗时→笔数」见证分布。"
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


@app.get("/health", summary="健康检查")
def health():
    return {"status": "ok"}
