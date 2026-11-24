"""
存档核对与流水段修复补传的端到端自测。

覆盖：
  1. 摘要手工对照（UTF-8 文本 `流水号,耗时\\n` 升序拼接，SHA-256）；
  2. 整段补齐：核对发现不一致 -> 签发 repair_id -> 补传验摘要 -> 整段替换，
     段内多出的旧流水被删除、段外补录保留、替换后按班报各档校验；
  3. 已补齐：库内段已与存档相符时补传返回 already_complete 且不改明细；
  4. 变动退回：核对后段被普通补交改动且仍不符 -> 补传退回（409），重新核对可修；
  5. 补传自身摘要不符 -> 拒绝（422）且整次不改库；
  6. 段内重复流水号、越界流水号 -> 422 不改库；
  7. 定档/档内容量校验失败 -> 整次回滚；
  8. 普通补交与修复交错：后写一方按先写入结果校验；
  9. 清单校验：重叠区间、坏摘要、空清单；
 10. 修复作业状态机：未知编号、superseded、completed；
 11. 稀疏段（区间有缺口）：整段替换删除段内多出旧号。
"""
from __future__ import annotations

import hashlib
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="archive-test-")
os.environ["APP_DB_PATH"] = os.path.join(_tmp, "archive.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import archive, service  # noqa: E402
from app.archive import segment_digest  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
client.__enter__()

# B0=[10,20] B1=[21,29] B2=[30,40] B3=[41,50]
BUCKETS = [
    {"lo": 10, "hi": 20}, {"lo": 21, "hi": 29},
    {"lo": 30, "hi": 40}, {"lo": 41, "hi": 50},
]


def make_report(shift_id, cap0=4, cap1=4):
    return {
        "buckets": BUCKETS,
        "terminals": [
            {"terminal_id": "T1", "bucket_idx": 0, "count": cap0},
            {"terminal_id": "T1", "bucket_idx": 1, "count": cap1},
            {"terminal_id": "T2", "bucket_idx": 2, "count": 2},
        ],
        "p50_ms": 20, "p95_ms": 41, "total_sum_ms": 300,
    }


def register(shift_id, **kw):
    r = client.put(f"/shifts/{shift_id}/report", json=make_report(shift_id, **kw))
    assert r.status_code == 200, r.text


def submit(shift_id, items, expected=200):
    r = client.post(f"/shifts/{shift_id}/details", json={"items": items})
    assert r.status_code == expected, r.text
    return r.json()


def detail_map(shift_id, terminal_id="T1"):
    rows = client.get(f"/shifts/{shift_id}/details").json()["items"]
    return {r["seq"]: r for r in rows if r["terminal_id"] == terminal_id}


def digest_pairs(pairs):
    """pairs: [(seq, value_ms), ...]，按 seq 升序算摘要。"""
    return segment_digest([{"seq": s, "value_ms": v} for s, v in pairs])


def reconcile(shift_id, terminal_id, segments, expected=200):
    r = client.post(
        f"/shifts/{shift_id}/terminals/{terminal_id}/reconcile",
        json={"segments": segments})
    assert r.status_code == expected, r.text
    return r.json()


def complete(repair_id, items, expected=200):
    r = client.post(f"/repairs/{repair_id}/complete",
                    json={"items": items})
    assert r.status_code == expected, r.text
    return r.json()


def repair_items(pairs):
    return [{"seq": s, "value_ms": v} for s, v in pairs]


# ---------------------------------------------------------------- 1. 摘要手工对照

def test_digest_manual_vectors():
    buf = "1,120\n2,130\n"
    want = hashlib.sha256(buf.encode("utf-8")).hexdigest()
    # 输入乱序也必须按流水号升序
    assert digest_pairs([(2, 130), (1, 120)]) == want
    # 单笔、UTF-8 文本格式
    assert digest_pairs([(7, 25)]) == \
        hashlib.sha256("7,25\n".encode("utf-8")).hexdigest()
    # 空段 -> 空文本摘要
    assert digest_pairs([]) == hashlib.sha256(b"").hexdigest()
    # 同值不同顺序拼接不同 -> 摘要不同
    assert digest_pairs([(1, 10), (2, 20)]) != \
        digest_pairs([(1, 20), (2, 10)])


# ---------------------------------------------------------------- 2. 整段补齐 happy path

def test_full_segment_repair_happy_path():
    shift = "H1"
    register(shift)
    # 存档"应有"的两段（权威值）
    truth_seg1 = [(1, 12), (2, 15), (3, 18)]          # 全在 B0
    truth_seg2 = [(10, 22), (11, 25), (12, 28)]       # 全在 B1
    d1 = digest_pairs(truth_seg1)
    d2 = digest_pairs(truth_seg2)

    # 本地明细：seg1 有错（seq3 写成 19 仍是 B0 但值不同），seg2 正确；
    # 另在段外有补录 seq20（B0），整段替换必须保留它
    submit(shift, [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 12},
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 0, "value_ms": 15},
        {"terminal_id": "T1", "seq": 3, "bucket_idx": 0, "value_ms": 19},
        {"terminal_id": "T1", "seq": 10, "bucket_idx": 1, "value_ms": 22},
        {"terminal_id": "T1", "seq": 11, "bucket_idx": 1, "value_ms": 25},
        {"terminal_id": "T1", "seq": 12, "bucket_idx": 1, "value_ms": 28},
        {"terminal_id": "T1", "seq": 20, "bucket_idx": 0, "value_ms": 11},
    ])

    res = reconcile(shift, "T1", [
        {"segment_no": "S1", "seq_lo": 1, "seq_hi": 3, "sha256": d1},
        {"segment_no": "S2", "seq_lo": 10, "seq_hi": 12, "sha256": d2},
    ])
    assert res["all_match"] is False
    assert res["mismatch_count"] == 1
    s1, s2 = res["segments"]
    assert s1["match"] is False and s1["repair_id"]
    assert s2["match"] is True and "repair_id" not in s2
    # 缺号检测（这里无缺号）
    assert s1["missing_seqs"] == [] and s1["local_count"] == 3
    repair_id = s1["repair_id"]

    # 作业可查询，状态 open
    jobs = client.get(f"/shifts/{shift}/repairs").json()["jobs"]
    job = next(j for j in jobs if j["repair_id"] == repair_id)
    assert job["status"] == "open" and job["seq_lo"] == 1 and job["seq_hi"] == 3

    # 凭编号补传 seg1 完整明细（不按升序也应接受，服务端排序）
    out = complete(repair_id, repair_items(
        [(3, 18), (1, 12), (2, 15)]))
    assert out["outcome"] == "repaired" and out["replaced"] is True
    # 段外全部保留：另一段 seq10-12（3 笔）+ 段外补录 seq20（1 笔）
    assert out["outside_preserved"] == 4
    assert out["stored_sha256"] == d1
    # 替换后该终端各档笔数：B0 = seq1,2,3,20 共4；B1 = seq10,11,12 共3
    counts = {c["bucket_idx"]: c["count"]
              for c in out["terminal_bucket_counts"]}
    assert counts == {0: 4, 1: 3}

    # 库里段 1 已与存档一致，段外 seq20 原样保留
    cur = detail_map(shift)
    assert cur[3]["value_ms"] == 18
    assert cur[20]["value_ms"] == 11
    assert segment_digest(
        [{"seq": r["seq"], "value_ms": r["value_ms"]}
         for r in cur.values() if 1 <= r["seq"] <= 3]) == d1

    # 再次核对：全部一致，不再签发作业
    again = reconcile(shift, "T1", [
        {"segment_no": "S1", "seq_lo": 1, "seq_hi": 3, "sha256": d1},
        {"segment_no": "S2", "seq_lo": 10, "seq_hi": 12, "sha256": d2},
    ])
    assert again["all_match"] is True and again["mismatch_count"] == 0

    # 同一 repair_id 再补传：作业已完成且段仍相符 -> already_complete
    out2 = complete(repair_id, repair_items(truth_seg1))
    assert out2["outcome"] == "already_complete" and out2["replaced"] is False


# ---------------------------------------------------------------- 3. 已补齐（普通补交先修好）

def test_already_complete_when_normal_submit_fixed_it():
    shift = "H2"
    register(shift)
    truth = [(1, 12), (2, 15)]
    d = digest_pairs(truth)
    # 本地是错的
    submit(shift, [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 20},
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 0, "value_ms": 20},
    ])
    res = reconcile(shift, "T1", [
        {"segment_no": "A", "seq_lo": 1, "seq_hi": 2, "sha256": d}])
    rid = res["segments"][0]["repair_id"]

    # 核对之后、补传之前：普通补交把该段更正到与存档一致
    submit(shift, [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 12},
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 0, "value_ms": 15},
    ])
    out = complete(rid, repair_items(truth))
    assert out["outcome"] == "already_complete" and out["replaced"] is False
    # 作业状态被置为 completed
    job = next(j for j in client.get(f"/shifts/{shift}/repairs").json()["jobs"]
               if j["repair_id"] == rid)
    assert job["status"] == "completed"
    cur = detail_map(shift)
    assert cur[1]["value_ms"] == 12 and cur[2]["value_ms"] == 15


# ---------------------------------------------------------------- 4. 变动退回

def test_changed_since_reconcile_is_rejected():
    shift = "H3"
    register(shift)
    truth = [(1, 12), (2, 15), (3, 18)]
    d = digest_pairs(truth)
    # 本地：seq1 错（13），其余对
    submit(shift, [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 13},
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 0, "value_ms": 15},
        {"terminal_id": "T1", "seq": 3, "bucket_idx": 0, "value_ms": 18},
    ])
    res = reconcile(shift, "T1", [
        {"segment_no": "A", "seq_lo": 1, "seq_hi": 3, "sha256": d}])
    rid = res["segments"][0]["repair_id"]
    snapshot = res["segments"][0]["actual_sha256"]

    # 核对后普通补交改动该段（seq2 15->16），仍与存档不符
    submit(shift, [
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 0, "value_ms": 16}])

    # 补传权威内容：自身摘要正确，但库内相对核对时已变动 -> 409 退回
    r = client.post(f"/repairs/{rid}/complete",
                    json={"items": repair_items(truth)})
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["rejected"] is True
    assert body["errors"][0]["current_sha256"] != snapshot
    # 明细未被改动（seq2 仍是 16）
    assert detail_map(shift)[2]["value_ms"] == 16

    # 重新核对后获得新编号，再补传即可修复
    res2 = reconcile(shift, "T1", [
        {"segment_no": "A", "seq_lo": 1, "seq_hi": 3, "sha256": d}])
    rid2 = res2["segments"][0]["repair_id"]
    assert rid2 != rid
    out = complete(rid2, repair_items(truth))
    assert out["outcome"] == "repaired"
    cur = detail_map(shift)
    assert [cur[s]["value_ms"] for s in (1, 2, 3)] == [12, 15, 18]

    # 旧编号已被新核对取代 -> superseded，再用被 409
    r = client.post(f"/repairs/{rid}/complete",
                    json={"items": repair_items(truth)})
    assert r.status_code == 409


# ---------------------------------------------------------------- 5. 补传摘要不符

def test_upload_digest_mismatch_rejected():
    shift = "H4"
    register(shift)
    truth = [(1, 12), (2, 15)]
    d = digest_pairs(truth)
    submit(shift, [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 20},
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 0, "value_ms": 20},
    ])
    rid = reconcile(shift, "T1", [
        {"segment_no": "A", "seq_lo": 1, "seq_hi": 2, "sha256": d}
    ])["segments"][0]["repair_id"]

    before = dict(detail_map(shift))
    # 补传内容里 seq2 传错（16 仍是 B0 档内值，但摘要对不上）
    r = client.post(f"/repairs/{rid}/complete",
                    json={"items": repair_items([(1, 12), (2, 16)])})
    assert r.status_code == 422
    assert r.json()["detail"]["rejected"] is True
    err = r.json()["detail"]["errors"][0]
    assert "摘要" in err["error"]
    assert err["upload_sha256"] != d
    # 整次不改库
    assert detail_map(shift) == before
    # 作业仍 open，可再次正确补传
    out = complete(rid, repair_items(truth))
    assert out["outcome"] == "repaired"


# ---------------------------------------------------------------- 6. 段内重复 / 越界流水号

def test_duplicate_and_out_of_range_seq_rejected():
    shift = "H5"
    register(shift)
    truth = [(1, 12), (2, 15), (3, 18)]
    d = digest_pairs(truth)
    submit(shift, [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 20},
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 0, "value_ms": 20},
        {"terminal_id": "T1", "seq": 3, "bucket_idx": 0, "value_ms": 20},
    ])
    rid = reconcile(shift, "T1", [
        {"segment_no": "A", "seq_lo": 1, "seq_hi": 3, "sha256": d}
    ])["segments"][0]["repair_id"]

    # 重复流水号
    r = client.post(f"/repairs/{rid}/complete", json={"items": repair_items(
        [(1, 12), (2, 15), (2, 18)])})
    assert r.status_code == 422 and r.json()["detail"]["rejected"]
    assert any("重复" in e["error"]
               for e in r.json()["detail"]["errors"])

    # 越界流水号（seq9 不在 [1,3]）
    r = client.post(f"/repairs/{rid}/complete", json={"items": repair_items(
        [(1, 12), (2, 15), (9, 18)])})
    assert r.status_code == 422
    assert any("不在本段" in e["error"]
               for e in r.json()["detail"]["errors"])

    # 空补传
    r = client.post(f"/repairs/{rid}/complete", json={"items": []})
    assert r.status_code == 422

    # 数据未受影响
    assert all(r["value_ms"] == 20 for r in detail_map(shift).values())


# ---------------------------------------------------------------- 7. 容量校验失败整次回滚

def test_capacity_violation_rolls_back():
    shift = "H6"
    # B0 容量仅 3
    register(shift, cap0=3, cap1=4)
    # 段外已有 1 笔 B0（seq20）
    submit(shift, [
        {"terminal_id": "T1", "seq": 20, "bucket_idx": 0, "value_ms": 11},
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 20},
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 0, "value_ms": 20},
    ])
    # 权威段：3 笔全在 B0；连同段外 seq20 -> B0 共 4 > 容量 3
    truth = [(1, 12), (2, 15), (3, 18)]
    d = digest_pairs(truth)
    rid = reconcile(shift, "T1", [
        {"segment_no": "A", "seq_lo": 1, "seq_hi": 3, "sha256": d}
    ])["segments"][0]["repair_id"]

    before = dict(detail_map(shift))
    r = client.post(f"/repairs/{rid}/complete",
                    json={"items": repair_items(truth)})
    assert r.status_code == 422 and r.json()["detail"]["rejected"]
    assert any("超过班报" in e["error"]
               for e in r.json()["detail"]["errors"])
    # 整次回滚：库内保持原样（seq3 仍不存在，seq1/2 仍是 20）
    assert detail_map(shift) == before
    # 作业仍 open
    job = next(j for j in client.get(f"/shifts/{shift}/repairs").json()["jobs"]
               if j["repair_id"] == rid)
    assert job["status"] == "open"


def test_value_outside_any_bucket_rejected():
    shift = "H7"
    register(shift)
    # 构造一个摘要，使补传内容里出现不落在任何分档的值（如 999）
    truth = [(1, 12), (2, 999)]
    d = digest_pairs(truth)
    submit(shift, [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 20},
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 0, "value_ms": 20},
    ])
    rid = reconcile(shift, "T1", [
        {"segment_no": "A", "seq_lo": 1, "seq_hi": 2, "sha256": d}
    ])["segments"][0]["repair_id"]
    r = client.post(f"/repairs/{rid}/complete",
                    json={"items": repair_items(truth)})
    assert r.status_code == 422
    assert any("无法定档" in e["error"]
               for e in r.json()["detail"]["errors"])


# ---------------------------------------------------------------- 8. 普通补交与修复交错

def test_interleaved_normal_submit_and_repair():
    shift = "H8"
    register(shift)
    truth_seg = [(1, 12), (2, 15)]          # B0
    d = digest_pairs(truth_seg)
    # 初始：段内 seq1 错；段外无
    submit(shift, [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 20},
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 0, "value_ms": 15},
    ])
    rid = reconcile(shift, "T1", [
        {"segment_no": "A", "seq_lo": 1, "seq_hi": 2, "sha256": d}
    ])["segments"][0]["repair_id"]

    # 先写入（普通补交）：在段外补录 seq30（B1），不影响段内快照
    submit(shift, [
        {"terminal_id": "T1", "seq": 30, "bucket_idx": 1, "value_ms": 22}])

    # 后写入（修复）：段内快照未变 -> 允许；容量按先写入结果校验，
    # B0 段内 2 笔 + 段外无 B0 其它 = 2 <= 4，通过；段外 seq30 保留
    out = complete(rid, repair_items(truth_seg))
    assert out["outcome"] == "repaired"
    assert out["outside_preserved"] == 1
    cur = detail_map(shift)
    assert cur[1]["value_ms"] == 12 and cur[2]["value_ms"] == 15
    assert cur[30]["value_ms"] == 22 and cur[30]["bucket_idx"] == 1

    # 反向交错：修复先写入后，普通补交（后写方）须按修复后的结果做容量校验
    # B0 现已有 seq1,2 两笔；再补交 3 笔不同流水号到 B0 -> 共 5 > 容量 4 -> 退回
    r = client.post(f"/shifts/{shift}/details", json={"items": [
        {"terminal_id": "T1", "seq": s, "bucket_idx": 0, "value_ms": 10}
        for s in (40, 41, 42)]})
    assert r.status_code == 422 and r.json()["detail"]["rejected"]
    # 修复结果与段外补录均未被破坏
    cur = detail_map(shift)
    assert cur[1]["value_ms"] == 12 and cur[30]["value_ms"] == 22
    assert 40 not in cur and 41 not in cur and 42 not in cur


# ---------------------------------------------------------------- 9. 清单校验

def test_manifest_validation():
    shift = "H9"
    register(shift)
    good = "a" * 64

    # 重叠区间
    r = client.post(
        f"/shifts/{shift}/terminals/T1/reconcile",
        json={"segments": [
            {"segment_no": "a", "seq_lo": 1, "seq_hi": 5, "sha256": good},
            {"segment_no": "b", "seq_lo": 5, "seq_hi": 9, "sha256": good},
        ]})
    assert r.status_code == 422
    assert any("重叠" in e["error"] for e in r.json()["detail"])

    # 坏摘要
    r = client.post(
        f"/shifts/{shift}/terminals/T1/reconcile",
        json={"segments": [
            {"segment_no": "a", "seq_lo": 1, "seq_hi": 5, "sha256": "xyz"}]})
    assert r.status_code == 422

    # 空清单
    r = client.post(
        f"/shifts/{shift}/terminals/T1/reconcile", json={"segments": []})
    assert r.status_code == 422

    # 未声明终端
    r = client.post(
        f"/shifts/{shift}/terminals/T9/reconcile",
        json={"segments": [
            {"segment_no": "a", "seq_lo": 1, "seq_hi": 5, "sha256": good}]})
    assert r.status_code == 422

    # 班次不存在 -> 404
    r = client.post(
        "/shifts/NOPE/terminals/T1/reconcile",
        json={"segments": [
            {"segment_no": "a", "seq_lo": 1, "seq_hi": 5, "sha256": good}]})
    assert r.status_code == 404


# ---------------------------------------------------------------- 10. 作业状态机 / 未知编号

def test_repair_job_lifecycle_and_unknown():
    # 未知修复编号
    r = client.post("/repairs/doesnotexist/complete",
                    json={"items": [{"seq": 1, "value_ms": 12}]})
    assert r.status_code == 404

    shift = "H10"
    register(shift)
    truth = [(1, 12)]
    d = digest_pairs(truth)
    submit(shift, [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 20}])
    rid1 = reconcile(shift, "T1", [
        {"segment_no": "A", "seq_lo": 1, "seq_hi": 1, "sha256": d}
    ])["segments"][0]["repair_id"]

    # 再次核对（仍不一致）-> 旧作业 superseded，签发新作业
    rid2 = reconcile(shift, "T1", [
        {"segment_no": "A", "seq_lo": 1, "seq_hi": 1, "sha256": d}
    ])["segments"][0]["repair_id"]
    assert rid1 != rid2
    jobs = {j["repair_id"]: j["status"]
            for j in client.get(f"/shifts/{shift}/repairs").json()["jobs"]}
    assert jobs[rid1] == "superseded" and jobs[rid2] == "open"

    # 旧作业补传被 409
    r = client.post(f"/repairs/{rid1}/complete",
                    json={"items": repair_items(truth)})
    assert r.status_code == 409

    # 新作业正常修复
    out = complete(rid2, repair_items(truth))
    assert out["outcome"] == "repaired"
    jobs = {j["repair_id"]: j["status"]
            for j in client.get(f"/shifts/{shift}/repairs").json()["jobs"]}
    assert jobs[rid2] == "completed"


# ---------------------------------------------------------------- 11. 稀疏段：删除段内多出的旧号

def test_sparse_segment_deletes_extra_old_seqs():
    shift = "H11"
    register(shift)
    # 权威稀疏段 [1,4] 只有 seq1、seq4 两笔（seq2/3 是缺口）
    truth = [(1, 12), (4, 18)]
    d = digest_pairs(truth)
    # 本地：seq1 值错；段内有多余旧号 seq3（须删除）；权威里的 seq4 缺失
    submit(shift, [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 20},
        {"terminal_id": "T1", "seq": 3, "bucket_idx": 0, "value_ms": 20},
    ])
    res = reconcile(shift, "T1", [
        {"segment_no": "SP", "seq_lo": 1, "seq_hi": 4, "sha256": d}])
    row = res["segments"][0]
    assert row["match"] is False
    assert row["missing_seqs"] == [2, 4]        # 本地只有 seq1、seq3
    rid = row["repair_id"]

    out = complete(rid, repair_items(truth))
    assert out["outcome"] == "repaired"
    assert out["deleted_and_replaced"] == 2      # 段内旧 2 笔（含多余 seq3）被替换
    cur = detail_map(shift)
    assert sorted(cur) == [1, 4]                 # 多余的 seq3 已删除、seq4 已补入
    assert cur[1]["value_ms"] == 12 and cur[4]["value_ms"] == 18
    assert segment_digest(
        [{"seq": r["seq"], "value_ms": r["value_ms"]}
         for r in cur.values()]) == d


# ---------------------------------------------------------------- 12. 自动定档：补传只给 seq + 耗时

def test_repair_auto_assigns_bucket():
    shift = "H12"
    register(shift)
    # 权威段跨两个分档：seq1 B0(12)、seq2 B1(25)
    truth = [(1, 12), (2, 25)]
    d = digest_pairs(truth)
    submit(shift, [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 20},
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 1, "value_ms": 29},
    ])
    rid = reconcile(shift, "T1", [
        {"segment_no": "A", "seq_lo": 1, "seq_hi": 2, "sha256": d}
    ])["segments"][0]["repair_id"]
    out = complete(rid, repair_items(truth))     # 不传 bucket_idx
    assert out["outcome"] == "repaired"
    cur = detail_map(shift)
    assert cur[1]["bucket_idx"] == 0 and cur[1]["value_ms"] == 12
    assert cur[2]["bucket_idx"] == 1 and cur[2]["value_ms"] == 25
    counts = {c["bucket_idx"]: c["count"]
              for c in out["terminal_bucket_counts"]}
    assert counts == {0: 1, 1: 1}




# ---------------------------------------------------------------- 13. 并发交错：修复与普通补交同时提交
#
# 串行化由 Database 的全局 threading.Lock 保证。注意 FastAPI TestClient 的
# portal 不是线程安全的，跨真实线程调用会偶发静默丢请求，因此本测试全程直接
# 调用 service/db 层，不经 HTTP，以精确验证持久层的串行化语义。

def _svc_setup(db, shift, cap0=4):
    db.upsert_report(shift, {
        "buckets": BUCKETS,
        "terminals": [
            {"terminal_id": "T1", "bucket_idx": 0, "count": cap0},
            {"terminal_id": "T1", "bucket_idx": 1, "count": 4},
            {"terminal_id": "T2", "bucket_idx": 2, "count": 2},
        ],
        "p50_ms": 20, "p95_ms": 41, "total_sum_ms": 300})
    truth = [{"seq": 1, "value_ms": 12}, {"seq": 2, "value_ms": 15}]
    d = digest_pairs([(x["seq"], x["value_ms"]) for x in truth])
    service.submit_details(db, shift, [
        {"terminal_id": "T1", "seq": 1, "bucket_idx": 0, "value_ms": 20},
        {"terminal_id": "T1", "seq": 2, "bucket_idx": 0, "value_ms": 15}])
    rid = archive.reconcile(db, shift, "T1", [
        {"segment_no": "A", "seq_lo": 1, "seq_hi": 2, "sha256": d}
    ])["segments"][0]["repair_id"]
    return truth, d, rid


def test_interleaved_outside_segment_submit_then_repair_succeeds():
    """确定性顺序：段外普通补交先写 -> 修复后写，按先写结果校验，段外流水保留。"""
    db = app.state.db
    shift = "CC-SEQ"
    truth, d, rid = _svc_setup(db, shift)
    # 段外补录（seq40/41 在段 [1,2] 之外）先写
    service.submit_details(db, shift, [
        {"terminal_id": "T1", "seq": 40, "bucket_idx": 0, "value_ms": 10},
        {"terminal_id": "T1", "seq": 41, "bucket_idx": 0, "value_ms": 10}])
    # 修复后写：段内快照未被段外补录改变 -> 成功，段外两笔保留
    out = archive.complete_repair(db, rid, truth)
    assert out["outcome"] == "repaired" and out["outside_preserved"] == 2
    cur = {r["seq"]: r for r in db.list_details(shift, "T1")}
    assert sorted(s for s, r in cur.items() if r["bucket_idx"] == 0) == \
        [1, 2, 40, 41]
    assert cur[1]["value_ms"] == 12 and cur[2]["value_ms"] == 15
    assert digest_pairs([(s, cur[s]["value_ms"]) for s in (1, 2)]) == d


def test_concurrent_repair_and_submit_holds_invariants():
    """并发压测：无论调度如何，库始终满足串行化不变量，绝不脏写/部分写/超档。"""
    import threading

    db = app.state.db
    cap0 = 4
    for round_i in range(40):
        shift = f"CC{round_i}"
        truth, d, rid = _svc_setup(db, shift, cap0=cap0)
        box: dict[str, object] = {}

        def do_repair():
            try:
                box["repair"] = archive.complete_repair(
                    db, rid, truth)["outcome"]
            except Exception as exc:  # noqa: BLE001
                box["repair"] = "rejected"
                box["repair_err"] = type(exc).__name__

        def do_submit():
            # 段外补录两笔 B0（与段 [1,2] 不相交）；段内 2 + 段外 2 = 容量 4
            try:
                service.submit_details(db, shift, [
                    {"terminal_id": "T1", "seq": 40,
                     "bucket_idx": 0, "value_ms": 10},
                    {"terminal_id": "T1", "seq": 41,
                     "bucket_idx": 0, "value_ms": 10}])
                box["submit"] = "ok"
            except Exception as exc:  # noqa: BLE001
                box["submit"] = "rejected"
                box["submit_err"] = type(exc).__name__

        t1 = threading.Thread(target=do_repair)
        t2 = threading.Thread(target=do_submit)
        t1.start(); t2.start(); t1.join(); t2.join()

        cur = {r["seq"]: r for r in db.list_details(shift, "T1")}
        job = db.get_repair_job(rid)["status"]

        # 不变量 1：段外补录不依赖修复，两笔必完整落库
        assert box["submit"] == "ok" and {40, 41}.issubset(cur)
        # 不变量 2：B0 绝不超容量，且只有 2(仅段内) 或 4(段内+段外) 两种完整状态
        b0 = [s for s, r in cur.items() if r["bucket_idx"] == 0]
        assert len(b0) <= cap0 and len(b0) in (2, 4)
        # 不变量 3：修复结果与段内实际状态严格一致（成功即权威值+作业完成；
        #           退回即段内原样+作业仍 open），不存在"返回成功却没落库"
        if box["repair"] == "repaired":
            assert job == "completed"
            assert cur[1]["value_ms"] == 12 and cur[2]["value_ms"] == 15
            assert digest_pairs(
                [(s, cur[s]["value_ms"]) for s in (1, 2)]) == d
        else:
            assert job == "open"
            assert cur[1]["value_ms"] == 20 and cur[2]["value_ms"] == 15

    # 容量硬约束（确定性）：B0 满 4 后再补 1 笔必退回且不落库
    shift = "CCFULL"
    _svc_setup(db, shift, cap0=4)
    try:
        service.submit_details(db, shift, [
            {"terminal_id": "T1", "seq": s, "bucket_idx": 0, "value_ms": 10}
            for s in (3, 4, 5)])
        raise AssertionError("超档补交本应被拒绝")
    except service.ValidationError:
        pass
    assert 5 not in {r["seq"] for r in db.list_details(shift, "T1")}
