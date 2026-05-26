"""
SQLite state store for the briefing pipeline.

Tables:
  briefing_items  – all ingested items, one row per unique URL/fingerprint
  pack_runs       – log of every AM/PM pack that was built
  publish_runs    – log of NotebookLM publish attempts/results
  audio_email_runs – log of audio-link email notification attempts/results
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from typing import List

from .models import BriefingItem

_DEFAULT_DB = Path(__file__).parent.parent / "state.db"


def get_connection(db_path: Path = _DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path = _DEFAULT_DB) -> None:
    """Create tables if they don't exist. Safe to call on every run."""
    with get_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS briefing_items (
                item_id       TEXT PRIMARY KEY,
                title         TEXT NOT NULL,
                summary       TEXT NOT NULL,
                source        TEXT NOT NULL,
                source_date   TEXT NOT NULL,
                url           TEXT,
                score         REAL,
                rating        TEXT,
                why_bullets   TEXT,
                leader_move   TEXT,
                confidence    TEXT,
                stream        TEXT,
                is_strong     INTEGER DEFAULT 0,
                pack_assigned TEXT,
                ingested_at   TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pack_runs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date     TEXT NOT NULL,
                pack_type    TEXT NOT NULL,
                item_count   INTEGER,
                output_path  TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS publish_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date      TEXT NOT NULL,
                pack_type     TEXT NOT NULL,
                status        TEXT NOT NULL,
                notebook_url  TEXT,
                message       TEXT,
                output_path   TEXT,
                created_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audio_email_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date      TEXT NOT NULL,
                pack_type     TEXT NOT NULL,
                status        TEXT NOT NULL,
                from_addr     TEXT,
                to_addrs      TEXT,
                subject       TEXT,
                notebook_url  TEXT,
                error         TEXT,
                created_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def item_exists(item_id: str, db_path: Path = _DEFAULT_DB) -> bool:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM briefing_items WHERE item_id = ?", (item_id,)
        ).fetchone()
    return row is not None


def insert_item(item: BriefingItem, db_path: Path = _DEFAULT_DB) -> bool:
    d = item.to_dict()
    with get_connection(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM briefing_items WHERE item_id = ?",
            (item.item_id,),
        ).fetchone()

        if existing is not None:
            should_upgrade = (
                item.source in ("morning", "evening")
                and existing["source"] != item.source
            )
            if not should_upgrade:
                return False

            conn.execute(
                """
                UPDATE briefing_items
                SET title = ?,
                    summary = ?,
                    source = ?,
                    source_date = ?,
                    url = ?,
                    score = ?,
                    rating = ?,
                    why_bullets = ?,
                    leader_move = ?,
                    confidence = ?,
                    stream = ?,
                    is_strong = ?,
                    pack_assigned = ?,
                    ingested_at = ?
                WHERE item_id = ?
                """,
                (
                    d["title"], d["summary"], d["source"], d["source_date"],
                    d["url"], d["score"], d["rating"], json.dumps(d["why_bullets"]),
                    d["leader_move"], d["confidence"], d["stream"],
                    int(d["is_strong"]), d["pack_assigned"], d["ingested_at"],
                    d["item_id"],
                ),
            )
            conn.commit()
            return True

        conn.execute(
            """
            INSERT INTO briefing_items
                (item_id, title, summary, source, source_date, url, score,
                 rating, why_bullets, leader_move, confidence, stream,
                 is_strong, pack_assigned, ingested_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                d["item_id"], d["title"], d["summary"],
                d["source"], d["source_date"], d["url"], d["score"],
                d["rating"], json.dumps(d["why_bullets"]),
                d["leader_move"], d["confidence"], d["stream"],
                int(d["is_strong"]), d["pack_assigned"], d["ingested_at"],
            ),
        )
        conn.commit()
    return True


def update_item_stream(item_id: str, stream: str, db_path: Path = _DEFAULT_DB) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE briefing_items SET stream = ? WHERE item_id = ?",
            (stream, item_id),
        )
        conn.commit()


def assign_pack(item_id: str, pack: str, db_path: Path = _DEFAULT_DB) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE briefing_items SET pack_assigned = ? WHERE item_id = ?",
            (pack, item_id),
        )
        conn.commit()


def get_items_for_date(date: str, db_path: Path = _DEFAULT_DB) -> List[BriefingItem]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM briefing_items WHERE source_date = ? ORDER BY score DESC",
            (date,),
        ).fetchall()
    return [_row_to_item(r) for r in rows]


def get_all_items(db_path: Path = _DEFAULT_DB) -> List[BriefingItem]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM briefing_items ORDER BY source_date DESC, score DESC"
        ).fetchall()
    return [_row_to_item(r) for r in rows]


def get_items_in_date_window(
    run_date: str,
    lookback_days: int = 7,
    db_path: Path = _DEFAULT_DB,
) -> List[BriefingItem]:
    end_date = datetime.strptime(run_date, "%Y-%m-%d").date()
    start_date = end_date - timedelta(days=lookback_days)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM briefing_items
            WHERE source_date >= ? AND source_date < ?
            ORDER BY source_date DESC, score DESC
            """,
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
    return [_row_to_item(r) for r in rows]


def log_pack_run(
    run_date: str,
    pack_type: str,
    item_count: int,
    output_path: str,
    db_path: Path = _DEFAULT_DB,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO pack_runs (run_date, pack_type, item_count, output_path)
               VALUES (?, ?, ?, ?)""",
            (run_date, pack_type, item_count, output_path),
        )
        conn.commit()


def get_pack_runs(limit: int = 20, db_path: Path = _DEFAULT_DB) -> list:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM pack_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def log_publish_run(
    run_date: str,
    pack_type: str,
    status: str,
    notebook_url: str | None,
    message: str,
    output_path: str,
    db_path: Path = _DEFAULT_DB,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO publish_runs (run_date, pack_type, status, notebook_url, message, output_path)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_date, pack_type, status, notebook_url, message, output_path),
        )
        conn.commit()


def get_publish_runs(limit: int = 20, db_path: Path = _DEFAULT_DB) -> list:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM publish_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_successful_publish_runs(limit: int = 100, db_path: Path = _DEFAULT_DB) -> list:
    """Return the newest publish candidate per (run_date, pack_type).

    Feed candidates must already have a NotebookLM URL and must not be error rows.
    Audio readiness is checked separately by the podcast bridge before inclusion.
    """
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT p.*
            FROM publish_runs p
            JOIN (
                SELECT run_date, pack_type, MAX(id) AS max_id
                FROM publish_runs
                WHERE status != 'error' AND notebook_url IS NOT NULL
                GROUP BY run_date, pack_type
            ) latest
              ON p.id = latest.max_id
            ORDER BY p.run_date DESC, p.pack_type ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def log_audio_email_run(
    run_date: str,
    pack_type: str,
    status: str,
    from_addr: str | None,
    to_addrs: list[str] | None,
    subject: str | None,
    notebook_url: str | None,
    error: str | None,
    db_path: Path = _DEFAULT_DB,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO audio_email_runs
               (run_date, pack_type, status, from_addr, to_addrs, subject, notebook_url, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_date,
                pack_type,
                status,
                from_addr,
                json.dumps(to_addrs) if to_addrs is not None else None,
                subject,
                notebook_url,
                error,
            ),
        )
        conn.commit()


def get_audio_email_runs(limit: int = 20, db_path: Path = _DEFAULT_DB) -> list:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM audio_email_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        if d.get("to_addrs"):
            try:
                d["to_addrs"] = json.loads(d["to_addrs"])
            except json.JSONDecodeError:
                pass
        out.append(d)
    return out


def get_stats(db_path: Path = _DEFAULT_DB) -> dict:
    with get_connection(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM briefing_items").fetchone()[0]
        by_source = conn.execute(
            "SELECT source, COUNT(*) as n FROM briefing_items GROUP BY source"
        ).fetchall()
        by_stream = conn.execute(
            "SELECT stream, COUNT(*) as n FROM briefing_items GROUP BY stream"
        ).fetchall()
        by_date = conn.execute(
            "SELECT source_date, COUNT(*) as n FROM briefing_items "
            "GROUP BY source_date ORDER BY source_date DESC LIMIT 7"
        ).fetchall()
        publish_total = conn.execute("SELECT COUNT(*) FROM publish_runs").fetchone()[0]
        audio_email_total = conn.execute("SELECT COUNT(*) FROM audio_email_runs").fetchone()[0]
    return {
        "total_items": total,
        "total_publishes": publish_total,
        "total_audio_email_attempts": audio_email_total,
        "by_source": {r["source"]: r["n"] for r in by_source},
        "by_stream": {r["stream"] or "unclassified": r["n"] for r in by_stream},
        "by_date": {r["source_date"]: r["n"] for r in by_date},
    }


def _row_to_item(row: sqlite3.Row) -> BriefingItem:
    return BriefingItem(
        title=row["title"],
        summary=row["summary"],
        source=row["source"],
        source_date=row["source_date"],
        url=row["url"],
        score=row["score"],
        rating=row["rating"] or "Unknown",
        why_bullets=json.loads(row["why_bullets"]) if row["why_bullets"] else [],
        leader_move=row["leader_move"],
        confidence=row["confidence"],
        stream=row["stream"],
        is_strong=bool(row["is_strong"]),
        pack_assigned=row["pack_assigned"],
        ingested_at=row["ingested_at"],
    )

