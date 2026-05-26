#!/usr/bin/env python3
"""
Pipeline status viewer.

Usage:
  python show_status.py                    # summary stats
  python show_status.py --date 2026-03-09  # items for a specific date
  python show_status.py --runs             # recent pack build runs
  python show_status.py --publishes        # recent NotebookLM publish runs
"""
import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from pipeline.db import (
    get_audio_email_runs,
    get_items_for_date,
    get_pack_runs,
    get_publish_runs,
    get_stats,
    init_db,
)


def _section(title: str) -> None:
    print(f"\n{'-' * 50}")
    print(f"  {title}")
    print(f"{'-' * 50}")


def show_summary() -> None:
    stats = get_stats()
    _section("Pipeline Summary")
    print(f"  Total items in DB : {stats['total_items']}")
    print(f"  Publish attempts  : {stats['total_publishes']}")
    print(f"  Audio email logs  : {stats['total_audio_email_attempts']}")

    _section("Items by source")
    for src, n in sorted(stats["by_source"].items()):
        print(f"  {src:<15} {n}")

    _section("Items by stream")
    for stream, n in sorted(stats["by_stream"].items(), key=lambda x: -x[1]):
        print(f"  {stream:<35} {n}")

    _section("Items by date (last 7)")
    for date, n in sorted(stats["by_date"].items(), reverse=True):
        print(f"  {date}  {n}")

    print()


def show_items_for_date(date: str) -> None:
    items = get_items_for_date(date)
    _section(f"Items for {date} ({len(items)} total)")

    by_pack: dict[str, list] = {"AM": [], "PM": [], "unassigned": []}
    for item in items:
        key = item.pack_assigned or "unassigned"
        by_pack.setdefault(key, []).append(item)

    for pack_key in ["AM", "PM", "unassigned"]:
        pack_items = by_pack.get(pack_key, [])
        if not pack_items:
            continue
        print(f"\n  [{pack_key}]")
        for item in pack_items:
            score_tag = f"{item.score:.1f}" if item.score else "   "
            stream = (item.stream or "?")[:30]
            url_tag = "*" if item.is_strong else "o"
            print(f"    {url_tag} [{score_tag}] {item.title[:60]:<60}  {stream}")

    print()


def show_pack_runs(limit: int = 20) -> None:
    runs = get_pack_runs(limit=limit)
    _section(f"Recent pack runs (last {limit})")
    if not runs:
        print("  No pack runs yet.")
    for run in runs:
        print(
            f"  {run['run_date']}  {run['pack_type']}  "
            f"{run['item_count']:>3} items  {run['created_at']}"
        )
    print()


def show_publish_runs(limit: int = 20) -> None:
    runs = get_publish_runs(limit=limit)
    _section(f"Recent publish runs (last {limit})")
    if not runs:
        print("  No publish runs yet.")
    for run in runs:
        url = run.get("notebook_url") or "-"
        msg = (run.get("message") or "").replace("\n", " ")[:90]
        print(
            f"  {run['run_date']}  {run['pack_type']}  {run['status']:<7}  "
            f"{run['created_at']}\n"
            f"    url: {url}\n"
            f"    msg: {msg}"
        )
    print()


def show_audio_email_runs(limit: int = 20) -> None:
    runs = get_audio_email_runs(limit=limit)
    _section(f"Recent audio email runs (last {limit})")
    if not runs:
        print("  No audio email runs yet.")
    for run in runs:
        recipients = run.get("to_addrs") or []
        if isinstance(recipients, list):
            recipients = ", ".join(recipients)
        print(
            f"  {run['run_date']}  {run['pack_type']}  {run['status']:<22}  {run['created_at']}\n"
            f"    from: {run.get('from_addr') or '-'}\n"
            f"    to: {recipients or '-'}\n"
            f"    subject: {run.get('subject') or '-'}\n"
            f"    notebook: {run.get('notebook_url') or '-'}"
        )
        if run.get("error"):
            print(f"    error: {run['error']}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="NotebookLM pipeline status viewer")
    parser.add_argument("--date", help="Show items for a specific date (YYYY-MM-DD)")
    parser.add_argument("--runs", action="store_true", help="Show recent pack run log")
    parser.add_argument("--publishes", action="store_true", help="Show recent NotebookLM publish log")
    parser.add_argument("--emails", action="store_true", help="Show recent audio email notification log")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    init_db()

    if args.json:
        print(json.dumps(get_stats(), indent=2))
        return 0

    if args.date:
        show_items_for_date(args.date)
    elif args.runs:
        show_pack_runs()
    elif args.publishes:
        show_publish_runs()
    elif args.emails:
        show_audio_email_runs()
    else:
        show_summary()

    return 0


if __name__ == "__main__":
    sys.exit(main())

