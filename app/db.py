"""SQLite 持久层：班报口径定义 + 终端流水明细（同号可更正）+ 存档修复作业。"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Optional


class _RepairNotFound(Exception):
    """事务内发现修复编号不存在（在持锁事务中抛出，由上层转 404）。"""

    def __init__(self, repair_id: str):
        self.repair_id = repair_id
        super().__init__(repair_id)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS shift_report (
    shift_id      TEXT PRIMARY KEY,
    payload_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS detail (
    shift_id     TEXT NOT NULL,
    terminal_id  TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    bucket_idx   INTEGER NOT NULL,
    value_ms     INTEGER NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (shift_id, terminal_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_detail_shift
    ON detail(shift_id, terminal_id, bucket_idx);
CREATE TABLE IF NOT EXISTS repair_job (
    repair_id     TEXT PRIMARY KEY,
    shift_id      TEXT NOT NULL,
    terminal_id   TEXT NOT NULL,
    segment_no    TEXT NOT NULL,
    seq_lo        INTEGER NOT NULL,
    seq_hi        INTEGER NOT NULL,
    expected_digest TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_repair_lookup
    ON repair_job(shift_id, terminal_id, status);
"""


class Database:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---------------------------------------------------------- 班报
    def upsert_report(self, shift_id: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO shift_report(shift_id, payload_json) VALUES (?,?) "
                "ON CONFLICT(shift_id) DO UPDATE SET "
                "payload_json=excluded.payload_json, "
                "updated_at=datetime('now')",
                (shift_id, body))

    @staticmethod
    def _get_report_conn(conn: sqlite3.Connection,
                         shift_id: str) -> Optional[dict]:
        row = conn.execute(
            "SELECT payload_json FROM shift_report WHERE shift_id=?",
            (shift_id,)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def get_report(self, shift_id: str) -> Optional[dict]:
        with self._lock:
            return self._get_report_conn(self._conn, shift_id)

    def has_details(self, shift_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM detail WHERE shift_id=? LIMIT 1",
                (shift_id,)).fetchone()
        return row is not None

    # ---------------------------------------------------------- 明细
    @staticmethod
    def _list_details_conn(conn: sqlite3.Connection, shift_id: str,
                          terminal_id: Optional[str] = None) -> list[dict]:
        if terminal_id is None:
            rows = conn.execute(
                "SELECT terminal_id, seq, bucket_idx, value_ms "
                "FROM detail WHERE shift_id=? "
                "ORDER BY terminal_id, seq", (shift_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT terminal_id, seq, bucket_idx, value_ms "
                "FROM detail WHERE shift_id=? AND terminal_id=? "
                "ORDER BY seq", (shift_id, terminal_id)).fetchall()
        return [dict(r) for r in rows]

    def list_details(self, shift_id: str,
                     terminal_id: Optional[str] = None) -> list[dict]:
        with self._lock:
            return self._list_details_conn(self._conn, shift_id, terminal_id)

    @staticmethod
    def _get_repair_job_conn(conn: sqlite3.Connection,
                             repair_id: str) -> Optional[dict]:
        row = conn.execute(
            "SELECT repair_id, shift_id, terminal_id, segment_no, "
            "seq_lo, seq_hi, expected_digest, snapshot_digest, status "
            "FROM repair_job WHERE repair_id=?", (repair_id,)).fetchone()
        return dict(row) if row else None

    def get_repair_job(self, repair_id: str) -> Optional[dict]:
        with self._lock:
            return self._get_repair_job_conn(self._conn, repair_id)

    @staticmethod
    def _apply_segment_repair_conn(conn: sqlite3.Connection,
                                   shift_id: str, terminal_id: str,
                                   seq_lo: int, seq_hi: int,
                                   items: list[dict], repair_id: str,
                                   expected_digest: str) -> None:
        """连接级整段替换（不含事务，调用方必须处于 tx() 内）。"""
        conn.execute(
            "DELETE FROM detail WHERE shift_id=? AND terminal_id=? "
            "AND seq BETWEEN ? AND ?",
            (shift_id, terminal_id, seq_lo, seq_hi))
        conn.executemany(
            "INSERT INTO detail(shift_id, terminal_id, seq, bucket_idx, "
            "value_ms) VALUES (?,?,?,?,?)",
            [(shift_id, terminal_id, it["seq"],
              it["bucket_idx"], it["value_ms"]) for it in items])
        conn.execute(
            "UPDATE repair_job SET status='completed', "
            "expected_digest=?, updated_at=datetime('now') "
            "WHERE repair_id=?", (expected_digest, repair_id))

    def run_repair_tx(self, repair_id: str, validator) -> dict:
        """
        在**单个事务**内完成修复补传的 读->校验->整段替换：

        - 事务内读取作业、生效班报与库内段当前状态，交给 validator 决策；
        - validator 抛异常即随事务回滚，整次不改库；
        - validator 返回 (apply, payload)：apply=True 时在同一事务内整段替换，
          apply=False（已补齐）时仅把作业置 completed，明细不动。

        validator 签名：(conn, job, report, local_rows)
            -> (apply: bool, payload: dict, incoming: list[dict]|None)
        """
        with self.tx() as conn:
            job = self._get_repair_job_conn(conn, repair_id)
            if job is None:
                raise _RepairNotFound(repair_id)
            report = self._get_report_conn(conn, job["shift_id"])
            local = self._list_details_conn(conn, job["shift_id"],
                                            job["terminal_id"])
            apply, payload, incoming = validator(conn, job, report, local)
            if apply:
                self._apply_segment_repair_conn(
                    conn, job["shift_id"], job["terminal_id"],
                    job["seq_lo"], job["seq_hi"], incoming,
                    repair_id, job["expected_digest"])
            else:
                conn.execute(
                    "UPDATE repair_job SET status='completed', "
                    "updated_at=datetime('now') WHERE repair_id=?",
                    (repair_id,))
            return payload

    def replace_details(self, shift_id: str, items: list[dict]) -> None:
        """全量覆盖某班已提交明细（调用方已完成业务校验）。"""
        with self.tx() as conn:
            conn.execute("DELETE FROM detail WHERE shift_id=?", (shift_id,))
            conn.executemany(
                "INSERT INTO detail(shift_id, terminal_id, seq, bucket_idx, "
                "value_ms) VALUES (?,?,?,?,?)",
                [(shift_id, it["terminal_id"], it["seq"],
                  it["bucket_idx"], it["value_ms"]) for it in items])

    @staticmethod
    def _upsert_merged_conn(conn: sqlite3.Connection, shift_id: str,
                            merged: dict[tuple[str, int],
                                         tuple[int, int]]) -> None:
        """连接级整表 upsert（不含事务，调用方必须处于 tx() 内）。"""
        conn.executemany(
            "INSERT INTO detail(shift_id, terminal_id, seq, bucket_idx, "
            "value_ms) VALUES (?,?,?,?,?) "
            "ON CONFLICT(shift_id, terminal_id, seq) DO UPDATE SET "
            "bucket_idx=excluded.bucket_idx, value_ms=excluded.value_ms, "
            "updated_at=datetime('now')",
            [(shift_id, term, seq, b, v)
             for (term, seq), (b, v) in merged.items()])

    def submit_details_tx(self, shift_id: str, items: list[dict],
                          validator) -> dict:
        """
        在**单个事务**内完成普通补交的 读->合并->校验->写：

        - 事务内读取班报与当前全部明细，交给 validator 决策；
        - validator(conn, report, merged_rows) 抛异常即随事务回滚，整次不改库；
        - validator 返回 (merged, stats) 后在同一事务内整表 upsert。

        这保证普通补交与整段修复并发时，后写者基于先写者已提交的最新状态校验，
        不会用旧快照覆盖（消除写偏 / lost update）。
        """
        with self.tx() as conn:
            report = self._get_report_conn(conn, shift_id)
            current = self._list_details_conn(conn, shift_id)
            merged, stats = validator(conn, report, current)
            self._upsert_merged_conn(conn, shift_id, merged)
            return stats

    # ---------------------------------------------------------- 存档修复作业
    def create_repair_jobs(self, shift_id: str, terminal_id: str,
                           jobs: list[dict]) -> None:
        """
        一次核对的全部不一致段作为一组作业落库：
        先把该 (班次, 终端) 此前仍 open 的作业置为 superseded，再插入新作业。
        """
        with self.tx() as conn:
            conn.execute(
                "UPDATE repair_job SET status='superseded', "
                "updated_at=datetime('now') "
                "WHERE shift_id=? AND terminal_id=? AND status='open'",
                (shift_id, terminal_id))
            conn.executemany(
                "INSERT INTO repair_job(repair_id, shift_id, terminal_id, "
                "segment_no, seq_lo, seq_hi, expected_digest, "
                "snapshot_digest, status) VALUES (?,?,?,?,?,?,?,?, 'open')",
                [(j["repair_id"], shift_id, terminal_id, str(j["segment_no"]),
                  j["seq_lo"], j["seq_hi"], j["expected_digest"],
                  j["snapshot_digest"]) for j in jobs])

    def list_repair_jobs(self, shift_id: str,
                         terminal_id: Optional[str] = None) -> list[dict]:
        with self._lock:
            if terminal_id is None:
                rows = self._conn.execute(
                    "SELECT repair_id, shift_id, terminal_id, segment_no, "
                    "seq_lo, seq_hi, expected_digest, snapshot_digest, status "
                    "FROM repair_job WHERE shift_id=? "
                    "ORDER BY created_at, seq_lo", (shift_id,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT repair_id, shift_id, terminal_id, segment_no, "
                    "seq_lo, seq_hi, expected_digest, snapshot_digest, status "
                    "FROM repair_job WHERE shift_id=? AND terminal_id=? "
                    "ORDER BY seq_lo", (shift_id, terminal_id)).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
