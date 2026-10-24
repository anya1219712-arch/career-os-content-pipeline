"""Хранилище на SQLite: для прототипа и тестов. В проде — PostgreSQL в Selectel (РФ), схема та же."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rubric TEXT NOT NULL,
    user_id INTEGER,
    source_tag TEXT,
    created_at TEXT NOT NULL,
    consent_at TEXT,
    raw_input TEXT,          -- исходник: удаляется через RETENTION_DAYS
    real_story TEXT,         -- ответ «что ты сделал на самом деле» (тоже удаляется)
    anonymized TEXT,
    status TEXT NOT NULL,
    note TEXT,
    revoked_at TEXT,
    purged_at TEXT
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    created_at TEXT NOT NULL,
    model_draft TEXT,
    model_review TEXT,
    fact_key TEXT,
    texts TEXT NOT NULL,     -- JSON: {"tg": ..., "threads": ..., "vk": ...}
    problems TEXT,           -- JSON: список нарушений правил и проверки
    status TEXT NOT NULL,    -- checks_failed | waiting_author | author_rejected | waiting_editor | approved | rejected | published
    week TEXT,               -- ISO-неделя публикации, например 2026-W45
    editor_id INTEGER,
    card_path TEXT
);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    case_id INTEGER,
    step TEXT NOT NULL,
    detail TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iso_week(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # --- журнал: каждый шаг конвейера оставляет след, по нему считаем время в пилоте ---
    def log(self, case_id: int | None, step: str, detail: str = "") -> None:
        self.conn.execute("INSERT INTO log (ts, case_id, step, detail) VALUES (?,?,?,?)",
                          (now(), case_id, step, detail))
        self.conn.commit()

    # --- кейсы ---
    def add_case(self, rubric: str, raw_input: str, real_story: str = "", user_id: int | None = None,
                 source_tag: str = "", consent: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO cases (rubric, user_id, source_tag, created_at, consent_at, raw_input, real_story, status) "
            "VALUES (?,?,?,?,?,?,?, 'new')",
            (rubric, user_id, source_tag, now(), now() if consent else None, raw_input, real_story),
        )
        self.conn.commit()
        self.log(cur.lastrowid, "case_created", rubric)
        return cur.lastrowid

    def get_case(self, case_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()

    def update_case(self, case_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE cases SET {cols} WHERE id=?", (*fields.values(), case_id))
        self.conn.commit()

    def revoke_user(self, user_id: int) -> list[int]:
        """Отзыв согласия: стираем исходники и снимаем все черновики пользователя."""
        rows = self.conn.execute("SELECT id FROM cases WHERE user_id=? AND revoked_at IS NULL", (user_id,)).fetchall()
        ids = [r["id"] for r in rows]
        for cid in ids:
            self.conn.execute(
                "UPDATE cases SET raw_input=NULL, real_story=NULL, anonymized=NULL, status='revoked', revoked_at=? WHERE id=?",
                (now(), cid))
            self.conn.execute("UPDATE drafts SET status='rejected', texts='{}' WHERE case_id=?", (cid,))
            self.log(cid, "revoked")
        self.conn.commit()
        return ids

    def purge_old(self, days: int) -> int:
        """Удаляем исходники старше срока хранения (минимизация данных по 152-ФЗ)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        cur = self.conn.execute(
            "UPDATE cases SET raw_input=NULL, real_story=NULL, purged_at=? "
            "WHERE purged_at IS NULL AND raw_input IS NOT NULL AND created_at < ?",
            (now(), cutoff))
        self.conn.commit()
        return cur.rowcount

    # --- черновики ---
    def add_draft(self, case_id: int, texts: dict, problems: list[str], status: str,
                  model_draft: str = "", model_review: str = "", fact_key: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO drafts (case_id, created_at, model_draft, model_review, fact_key, texts, problems, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (case_id, now(), model_draft, model_review, fact_key,
             json.dumps(texts, ensure_ascii=False), json.dumps(problems, ensure_ascii=False), status))
        self.conn.commit()
        return cur.lastrowid

    def get_draft(self, draft_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()

    def update_draft(self, draft_id: int, **fields) -> None:
        if "texts" in fields and isinstance(fields["texts"], dict):
            fields["texts"] = json.dumps(fields["texts"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE drafts SET {cols} WHERE id=?", (*fields.values(), draft_id))
        self.conn.commit()

    def count_in_week(self, week: str, rubric: str | None = None) -> int:
        sql = ("SELECT COUNT(*) FROM drafts d JOIN cases c ON c.id=d.case_id "
               "WHERE d.week=? AND d.status IN ('approved','published')")
        args: list = [week]
        if rubric:
            sql += " AND c.rubric=?"
            args.append(rubric)
        return self.conn.execute(sql, args).fetchone()[0]

    def drafts_by_status(self, status: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM drafts WHERE status=? ORDER BY id", (status,)).fetchall()
