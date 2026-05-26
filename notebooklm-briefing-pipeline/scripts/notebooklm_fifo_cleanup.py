"""FIFO cleanup for generated NotebookLM briefing/research notebooks.

Deletes generated notebooks older than N days first, then (optionally) continues
FIFO until a keep-latest target is met. Intended to preserve fresh podcast/feed
items while making room under NotebookLM's notebook quota.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

import sys
sys.path.insert(0, str(ROOT))

from pipeline.notebooklm_adapter import NotebookLMAdapter  # noqa: E402


def parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    # notebooklm_tools may expose protobuf-ish seconds/nanos or ISO strings.
    if text.startswith("[") and text.endswith("]"):
        try:
            parts = json.loads(text)
            return datetime.fromtimestamp(float(parts[0]), tz=timezone.utc)
        except Exception:
            pass
    for fmt in (None, "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            if fmt is None:
                return datetime.fromisoformat(text.replace("Z", "+00:00"))
            dt = datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def notebook_created_at(nb: Any) -> datetime:
    return (
        parse_dt(getattr(nb, "created_at", None))
        or parse_dt(getattr(nb, "create_time", None))
        or parse_dt(getattr(nb, "modified_at", None))
        or datetime.min.replace(tzinfo=timezone.utc)
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--older-than-days", type=int, default=30)
    ap.add_argument("--keep-latest", type=int, default=None, help="Optional FIFO retention target after age cleanup")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    nb_cfg = dict(config.get("notebooklm", {}))
    prefixes = list(nb_cfg.get("generated_notebook_prefixes") or [])
    for prefix in ["AI Briefing ", "Research Paper", "Research Paper -", "Research Paper Deep Dive"]:
        if prefix not in prefixes:
            prefixes.append(prefix)
    nb_cfg["generated_notebook_prefixes"] = prefixes

    adapter = NotebookLMAdapter(nb_cfg)
    client = adapter._load_client()
    notebooks = list(client.list_notebooks() or [])
    generated = [nb for nb in notebooks if adapter._is_generated_notebook(nb)]
    generated.sort(key=notebook_created_at)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than_days)
    victims = [nb for nb in generated if notebook_created_at(nb) < cutoff]
    if args.keep_latest is not None:
        excess = max(0, len(generated) - args.keep_latest)
        fifo_victims = generated[:excess]
        seen = {getattr(nb, "id", None) for nb in victims}
        victims.extend(nb for nb in fifo_victims if getattr(nb, "id", None) not in seen)

    print(f"NotebookLM total={len(notebooks)} generated={len(generated)} cutoff={cutoff.isoformat()} victims={len(victims)} dry_run={args.dry_run}")
    for nb in victims:
        nbid = getattr(nb, "id", None)
        title = getattr(nb, "title", "")
        created = notebook_created_at(nb).isoformat()
        if not nbid:
            print(f"SKIP no-id {created} {title}")
            continue
        if args.dry_run:
            print(f"DRY DELETE {created} {nbid} {title}")
            continue
        ok = client.delete_notebook(nbid)
        print(f"DELETE {'OK' if ok else 'FAILED'} {created} {nbid} {title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

