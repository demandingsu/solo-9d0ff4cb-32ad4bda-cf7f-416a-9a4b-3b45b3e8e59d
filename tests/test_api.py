"""API 端到端自测：双口径均可行、明细更正后结论变化、整批退回、大笔数。"""
from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="shift-test-")
os.environ["APP_DB_PATH"] = os.path.join(_tmp, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)
client.__enter__()       # 触发 lifespan，初始化 SQLite

# 14 笔，分档互不重叠：B0=[10,20] B1=[21,29] B2=[30,40] B3=[41,50]
# N=14: floor k50=7 / ceil k50=7；floor k95=13 / ceil k95=14 —— P95 取位不同
REPORT = {
    "buckets": [
        {"lo": 10, "hi": 20}, {"lo": 21, "hi": 29},
        {"lo": 30, "hi": 40}, {"lo": 41, "hi": 50},
    ],
    "terminals": [
        {"terminal_id": "T1", "bucket_idx": 0, "count": 7},
        {"terminal_id": "T2", "bucket_idx": 1, "count": 2},
        {"terminal_id": "T3", "bucket_idx": 2, "count": 2},
        {"terminal_id": "T3", "bucket_idx": 3, "count": 1},
        {"terminal_id": "T4", "bucket_idx": 3, "count": 2},
    ],
    # N=14。初始固定 8 笔（和 215）：T1 3x20（B0 留 4 个自由）、
    # T2 21/22、T3#1 B2=30、T4 2x41（B3 容量填满，41 的数量完全由明细决定）
    # 补提交 T3#2 B3=41 后 41 值共 3 笔，两口径可行区间均含 S=370；
    # 再把 T3#2 从 B3(41) 更正到本终端已声明的 B2(30)：总和少 11，
    # ceil 可行区间上界降到 366（S=370 落空）-> ceil 失效；
    # floor 区间上界 375 仍含 370 -> floor 仍可行，结论因更正而变化
    "p50_ms": 20, "p95_ms": 41, "total_sum_ms": 370,
}


def _submit(items, expected_code=200):
    r = client.post("/shifts/A/details", json={"items": items})
    assert r.status_code == expected_code, r.text
    return r.json()


def _check():
    r = client.post("/shifts/A/check")
    assert r.status_code == 200, r.text
    return {c["rule"]: c for c in r.json()["conventions"]}


def _verify_witness(w, rule, report):
    """对见证分布做独立复算。"""
    from fractions import Fraction
    n = sum(t["count"] for t in report["terminals"])
    dist = {d["value_ms"]: d["count"] for d in w["distribution"]}
    assert sum(dist.values()) == n
    assert sum(v * c for v, c in dist.items()) == report["total_sum_ms"]
    cum = w["cumulative"]
    for name, p in (("P50", Fraction(1, 2)), ("P95", Fraction(19, 20))):
        pn = p * n
        if rule == "ceil":
            k = max(1, -(-pn.numerator // pn.denominator))
        else:
            k = max(1, pn.numerator // pn.denominator)
        want = report["p50_ms"] if name == "P50" else report["p95_ms"]
        got = next(row["value_ms"] for row in cum
                   if k <= row["cumulative_count"])
        assert got == want, (rule, name, k, got, want)
        # 第 k 项为 got 的充要条件：前 k-1 项 < got，第 k 项起为 got
        idx = next(i for i, row in enumerate(cum) if row["value_ms"] == got)
        before = 0 if idx == 0 else cum[idx - 1]["cumulative_count"]
        through = cum[idx]["cumulative_count"]
        assert before <= k - 1 < through


def _initial_items():
    return (
        [{"terminal_id": "T1", "seq": i + 1, "bucket_idx": 0,
          "value_ms": 20} for i in range(3)]
        + [{"terminal_id": "T2", "seq": i + 1, "bucket_idx": 1,
            "value_ms": v} for i, v in enumerate([21, 22])]
        + [{"terminal_id": "T3", "seq": 1, "bucket_idx": 2, "value_ms": 30},
           {"terminal_id": "T4", "seq": 1, "bucket_idx": 3, "value_ms": 41},
           {"terminal_id": "T4", "seq": 2, "bucket_idx": 3, "value_ms": 41}]
    )


def test_full_flow_both_conventions_then_correction_flips():
    # 1) 登记班报；重叠分档必须拒绝
    assert client.put("/shifts/A/report", json=REPORT).status_code == 200
    bad = dict(REPORT, buckets=[{"lo": 1, "hi": 10}, {"lo": 10, "hi": 20}])
    assert client.put("/shifts/A/report", json=bad).status_code == 422
    assert client.put("/shifts/A/report", json=REPORT).status_code == 200

    # 2) 提交 8 笔明细
    res = _submit(_initial_items())
    assert res["rejected"] is False and res["stored"] == 8

    # 3) 两种口径均可行，且见证可独立复算
    conv = _check()
    assert conv["ceil"]["feasible"] is True
    assert conv["floor"]["feasible"] is True
    # 两口径在 N=14 下 P95 取位确实不同
    assert conv["ceil"]["quantile_positions"][1]["k"] == 14
    assert conv["floor"]["quantile_positions"][1]["k"] == 13
    for rule in ("ceil", "floor"):
        _verify_witness(conv[rule]["witness"], rule, REPORT)
        rng = conv[rule]["feasible_sum_range_ms"]
        assert rng["min"] <= 370 <= rng["max"]
        wdist = {d["value_ms"]: d["count"]
                 for d in conv[rule]["witness"]["distribution"]}
        assert wdist[30] >= 1 and wdist[41] >= 1     # 固定明细不可被改动

    # 4) 整批退回：越界 + 未声明终端，任何一条都不得落库
    rej = _submit([
        {"terminal_id": "T1", "seq": 99, "bucket_idx": 0, "value_ms": 99},
        {"terminal_id": "T9", "seq": 1, "bucket_idx": 0, "value_ms": 10},
    ], expected_code=422)
    assert rej["detail"]["rejected"] is True
    assert len(client.get("/shifts/A/details").json()["items"]) == 8

    # 5) 补提交 T3#2 到本终端已声明的 B3=41（新流水号），两口径仍均可行
    res = _submit([{"terminal_id": "T3", "seq": 2, "bucket_idx": 3,
                    "value_ms": 41}])
    assert res["inserted"] == 1 and res["corrected"] == 0
    conv = _check()
    assert conv["ceil"]["feasible"] and conv["floor"]["feasible"]

    # 6) 更正：T3 流水号 2 从 B3(41) 改到本终端已声明的 B2(30)
    res = _submit([{"terminal_id": "T3", "seq": 2, "bucket_idx": 2,
                    "value_ms": 30}])
    assert res["corrected"] == 1 and res["inserted"] == 0
    stored = client.get("/shifts/A/details").json()["items"]
    t3 = sorted((x["bucket_idx"], x["value_ms"])
                for x in stored if x["terminal_id"] == "T3")
    assert t3 == [(2, 30), (2, 30)]
    t4 = sorted(x["value_ms"] for x in stored
                if x["terminal_id"] == "T4")
    assert t4 == [41, 41]                          # 41 值只剩 T4 这 2 笔

    # 7) 结论变化：ceil 的可行区间不再包含 S=370 而失效，floor 仍可行
    conv = _check()
    assert conv["floor"]["feasible"] is True
    assert conv["ceil"]["feasible"] is False
    assert "可行区间" in conv["ceil"]["reason"]
    _verify_witness(conv["floor"]["witness"], "floor", REPORT)

    # 8) 再更正回 B3=41，两种口径恢复均可行
    _submit([{"terminal_id": "T3", "seq": 2, "bucket_idx": 3,
              "value_ms": 41}])
    conv = _check()
    assert conv["ceil"]["feasible"] and conv["floor"]["feasible"]


def test_over_capacity_rejected():
    client.put("/shifts/B/report", json=REPORT)
    # T1 档内容量 7，提交 8 笔不同流水号 -> 退回且不落库
    items = [{"terminal_id": "T1", "seq": i + 1, "bucket_idx": 0,
              "value_ms": 15} for i in range(8)]
    r = client.post("/shifts/B/details", json={"items": items})
    assert r.status_code == 422 and r.json()["detail"]["rejected"]
    assert client.get("/shifts/B/details").json()["items"] == []


def test_huge_counts_compact_witness():
    report = {
        "buckets": [{"lo": 0, "hi": 10}, {"lo": 11, "hi": 20},
                    {"lo": 21, "hi": 30}],
        "terminals": [
            {"terminal_id": "T1", "bucket_idx": 0, "count": 6_000_000_000},
            {"terminal_id": "T2", "bucket_idx": 1, "count": 4_000_000_000},
            {"terminal_id": "T3", "bucket_idx": 2, "count": 2_000_000_000},
        ],
        "p50_ms": 10, "p95_ms": 21,
        "total_sum_ms": 150_000_000_000,
    }
    assert client.put("/shifts/C/report", json=report).status_code == 200
    r = client.post("/shifts/C/check")
    assert r.status_code == 200, r.text
    for conv in r.json()["conventions"]:
        assert conv["feasible"] is True, conv["reason"]
        w = conv["witness"]
        assert w["total_count"] == 12_000_000_000
        # 见证是聚合分布，行数远小于总笔数，绝不逐笔展开
        assert len(w["distribution"]) <= 12
        assert sum(d["count"] for d in w["distribution"]) == 12_000_000_000
        assert sum(d["value_ms"] * d["count"]
                   for d in w["distribution"]) == 150_000_000_000


def test_docs_and_health():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
