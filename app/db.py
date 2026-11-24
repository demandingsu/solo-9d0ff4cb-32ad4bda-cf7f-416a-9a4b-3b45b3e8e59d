"""SQLite 持久层：班报口径定义 + 终端流水明细（同号可更正）。"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Optional

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

    def get_report(self, shift_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM shift_report WHERE shift_id=?",
                (shift_id,)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def has_details(self, shift_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM detail WHERE shift_id=? LIMIT 1",
                (shift_id,)).fetchone()
        return row is not None

    # ---------------------------------------------------------- 明细
    def list_details(self, shift_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT terminal_id, seq, bucket_idx, value_ms "
                "FROM detail WHERE shift_id=? "
                "ORDER BY terminal_id, seq", (shift_id,)).fetchall()
        return [dict(r) for r in rows]

    def replace_details(self, shift_id: str, items: list[dict]) -> None:
        """全量覆盖某班已提交明细（调用方已完成业务校验）。"""
        with self.tx() as conn:
            conn.execute("DELETE FROM detail WHERE shift_id=?", (shift_id,))
            conn.executemany(
                "INSERT INTO detail(shift_id, terminal_id, seq, bucket_idx, "
                "value_ms) VALUES (?,?,?,?,?)",
                [(shift_id, it["terminal_id"], it["seq"],
                  it["bucket_idx"], it["value_ms"]) for it in items])

    def replace_merged(self, shift_id: str,
                       merged: dict[tuple[str, int], tuple[int, int]],
                       stats: dict) -> dict:
        """
        按 (terminal_id, seq) 合并后的完整状态一次性落库（调用方已完成校验）。
        同号覆盖即"更正"，新键即插入。
        """
        with self.tx() as conn:
            conn.executemany(
                "INSERT INTO detail(shift_id, terminal_id, seq, bucket_idx, "
                "value_ms) VALUES (?,?,?,?,?) "
                "ON CONFLICT(shift_id, terminal_id, seq) DO UPDATE SET "
                "bucket_idx=excluded.bucket_idx, value_ms=excluded.value_ms, "
                "updated_at=datetime('now')",
                [(shift_id, term, seq, b, v)
                 for (term, seq), (b, v) in merged.items()])
        return {"stored": len(merged), **stats}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
