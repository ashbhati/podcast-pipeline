#!/usr/bin/env python3
"""
NotebookLM Briefing Pipeline — Main Orchestrator
"""
import argparse
import json
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from pipeline.db import (
    assign_pack,
    get_items_in_date_window,
    get_items_for_date,
    init_db,
    insert_item,
    log_audio_email_run,
    log_pack_run,
    log_publish_run,
    update_item_stream,
)
from capture_raw_articles import capture_run as capture_raw_run
from pipeline.discord_adapter import DiscordIntakeAdapter
from pipeline.ingestion import parse_briefing_file
from pipeline.notebooklm_adapter import NotebookLMAdapter
from pipeline.pack_builder import build_packs
from pipeline.run_logging import PipelineRunLogger, config_summary

_CONFIG_PATH = _HERE / "config.json"
_WORKSPACE_ROOT = _HERE.parent
_TZ = ZoneInfo("America/New_York")
_DEFAULT_RAW_CAPTURE_ROOT = Path(r"C:\Users\you\Projects\SecondBrain\raw\news")


def load_config() -> dict:
    if _CONFIG_PATH.exists():
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    return {}


def discover_briefing_files(run_date: str) -> list[tuple[Path, str]]:
    candidates = [
        (f"morning_ai_briefing_{run_date}.txt", "morning"),
        (f"morning_briefing_{run_date}.txt", "morning"),
        (f"evening_ai_briefing_{run_date}.txt", "evening"),
        (f"evening_briefing_{run_date}.txt", "evening"),
    ]
    found = []
    for filename, source in candidates:
        p = _WORKSPACE_ROOT / filename
        if p.exists():
            found.append((p, source))
    return found


def compute_manual_window(run_date: str, mode: str, config: dict) -> tuple[datetime | None, datetime | None]:
    if mode == "ALL":
        return None, None

    windows = config.get("pipeline", {}).get("manual_intake_windows", {})
    window_cfg = windows.get(mode, {})
    if not window_cfg:
        return None, None

    run_day = datetime.strptime(run_date, "%Y-%m-%d").replace(tzinfo=_TZ)

    start_offset = int(window_cfg.get("start_day_offset", 0))
    start_hour, start_minute = [int(x) for x in window_cfg.get("start_time", "00:00").split(":", 1)]
    start_local = (run_day + timedelta(days=start_offset)).replace(
        hour=start_hour,
        minute=start_minute,
        second=0,
        microsecond=0,
    )

    end_offset = int(window_cfg.get("end_day_offset", 0))
    end_hour, end_minute = [int(x) for x in window_cfg.get("end_time", "23:59").split(":", 1)]
    end_local = (run_day + timedelta(days=end_offset)).replace(
        hour=end_hour,
        minute=end_minute,
        second=59,
        microsecond=0,
    )

    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))


def _publish_one(adapter: NotebookLMAdapter, pack_type: str, path: Path, run_date: str) -> tuple[bool, str | None, str, str]:
    label = f"AI Briefing {pack_type} - {run_date}"
    try:
        url, audio_status = adapter.publish_pack(path, label)
        if audio_status == "quota_reached":
            msg = f"NOTEBOOKLM_{pack_type}_READY_NO_AUDIO: {url} (quota reached)"
            status = "published_no_audio"
        else:
            msg = f"NOTEBOOKLM_{pack_type}_READY: {url}"
            status = "published_audio_requested"
        log_publish_run(run_date, pack_type, status, url, msg, str(path))
        return True, url, msg, audio_status
    except Exception as exc:
        msg = f"NOTEBOOKLM_{pack_type}_ERROR: {str(exc).strip()}"
        log_publish_run(run_date, pack_type, "error", None, msg, str(path))
        return False, None, msg, "error"


def _notify_audio_link(run_date: str, pack_type: str, notebook_url: str, config: dict) -> bool:
    notify_cfg = config.get("notifications", {}).get("audio_email", {})
    if not notify_cfg.get("enabled", False):
        log_audio_email_run(
            run_date,
            pack_type,
            "skipped_disabled",
            None,
            None,
            None,
            notebook_url,
            "audio_email notification disabled in config",
        )
        return False

    to_addrs = notify_cfg.get("to", [])
    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]
    to_addrs = [x.strip() for x in to_addrs if str(x).strip()]
    if not to_addrs:
        log_audio_email_run(
            run_date,
            pack_type,
            "skipped_no_recipients",
            None,
            [],
            None,
            notebook_url,
            "no recipients configured",
        )
        return False

    smtp_cfg = notify_cfg.get("smtp", {})
    host = smtp_cfg.get("host", "smtp.gmail.com")
    port = int(smtp_cfg.get("port", 587))
    username = smtp_cfg.get("username")
    password = smtp_cfg.get("password")
    from_addr = smtp_cfg.get("from", username)

    if not username or not password or not from_addr:
        print("  [Notify] audio email skipped: missing SMTP credentials")
        log_audio_email_run(
            run_date,
            pack_type,
            "skipped_missing_smtp",
            from_addr,
            to_addrs,
            None,
            notebook_url,
            "missing SMTP credentials",
        )
        return False

    subject = f"NotebookLM {pack_type} Audio Briefing Ready — {run_date}"
    body = (
        f"NotebookLM {pack_type} audio briefing has been generated for {run_date}.\n\n"
        f"Open briefing: {notebook_url}\n\n"
        "Note: Access depends on NotebookLM account permissions for this notebook."
    )

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject

    try:
        with smtplib.SMTP(host, port, timeout=40) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(from_addr, to_addrs, msg.as_string())
        log_audio_email_run(
            run_date,
            pack_type,
            "sent",
            from_addr,
            to_addrs,
            subject,
            notebook_url,
            None,
        )
        print(f"  [Notify] audio link email sent to {', '.join(to_addrs)}")
        return True
    except Exception as exc:
        log_audio_email_run(
            run_date,
            pack_type,
            "error",
            from_addr,
            to_addrs,
            subject,
            notebook_url,
            str(exc),
        )
        print(f"  [Notify] audio email failed: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="NotebookLM briefing pipeline")
    parser.add_argument("--date", default=datetime.today().strftime("%Y-%m-%d"), help="Date to process (YYYY-MM-DD)")
    parser.add_argument("--file", nargs="+", metavar="PATH", help="Explicit briefing file path(s) to ingest")
    parser.add_argument("--skip-discord", action="store_true", help="Skip Discord intake fetch")
    parser.add_argument("--dry-run", action="store_true", help="Ingest only; do not build or publish packs")
    parser.add_argument("--mode", choices=["ALL", "AM", "PM"], default="ALL", help="Run full pipeline, or only publish the AM / PM pack")
    args = parser.parse_args()

    config = load_config()
    run_date = args.date
    run_logger = PipelineRunLogger(_HERE, run_date, args.mode)
    run_logger.start(argv=sys.argv, config_summary=config_summary(config))

    print(f"\n=== NotebookLM Briefing Pipeline ===")
    print(f"Date : {run_date}")
    print(f"Mode : {'dry-run' if args.dry_run else args.mode}")
    print(f"Log  : {run_logger.path}")
    print()

    with run_logger.step("database_init"):
        init_db()
    print("[1/5] Database ready")

    if args.file:
        files_to_ingest = []
        for f in args.file:
            p = Path(f)
            if not p.exists():
                p = _WORKSPACE_ROOT / f
            if p.exists():
                src = "evening" if "evening" in p.name.lower() else "morning"
                files_to_ingest.append((p, src))
            else:
                print(f"  WARNING: file not found — {f}")
    else:
        files_to_ingest = discover_briefing_files(run_date)

    print(f"[2/5] Ingesting {len(files_to_ingest)} briefing file(s)...")
    total_new = total_dupes = 0
    discovered_counts = {"morning": 0, "evening": 0}

    with run_logger.step("ingest_briefing_files", file_count=len(files_to_ingest)):
        for file_path, source in files_to_ingest:
            if source in discovered_counts:
                discovered_counts[source] += 1
            items = parse_briefing_file(file_path, source=source, source_date=run_date)
            new = dupes = 0
            for item in items:
                if insert_item(item):
                    new += 1
                else:
                    dupes += 1
            total_new += new
            total_dupes += dupes
            run_logger.event(
                "briefing_file_ingested",
                status="ok",
                path=str(file_path),
                source=source,
                parsed_count=len(items),
                new_count=new,
                duplicate_count=dupes,
            )
            print(f"  {file_path.name}: {len(items)} parsed -> {new} new, {dupes} dupes")

    if args.skip_discord:
        print("[3/5] Discord intake: skipped (--skip-discord)")
        run_logger.event("discord_intake_skipped", status="skipped", reason="--skip-discord")
    else:
        print("[3/5] Fetching Discord intake...")
        with run_logger.step("discord_intake"):
            discord_cfg = config.get("discord", {})
            discord = DiscordIntakeAdapter(discord_cfg, run_logger=run_logger)
            since_dt, until_dt = compute_manual_window(run_date, args.mode, config)
            if since_dt and until_dt:
                print(f"  Manual window ({args.mode}): {since_dt.isoformat()} -> {until_dt.isoformat()}")
            discord_items = discord.fetch_recent_items(
                since_hours=24,
                since_dt=since_dt,
                until_dt=until_dt,
                source_date=run_date,
            )
            for item in discord_items:
                if insert_item(item):
                    total_new += 1
                else:
                    total_dupes += 1
            run_logger.event(
                "discord_intake_finish",
                status="ok",
                fetched_count=len(discord_items),
                total_new=total_new,
                total_duplicates=total_dupes,
            )
            if discord_items:
                print(f"  Discord: {len(discord_items)} items fetched")

    print(f"  Total: {total_new} new items, {total_dupes} duplicates skipped")

    if args.dry_run:
        print("\n[dry-run] Stopping before pack build. DB updated.")
        run_logger.finish(status="ok", summary="dry-run completed before pack build")
        return 0

    print("[4/5] Building AM/PM packs...")
    all_items = get_items_for_date(run_date)
    if not all_items:
        msg = "No items in DB for this date. Run with --file or check briefing files."
        print(f"  {msg}")
        if args.mode == "AM":
            print(f"NOTEBOOKLM_AM_ERROR: {msg}")
        elif args.mode == "PM":
            print(f"NOTEBOOKLM_PM_ERROR: {msg}")
        run_logger.finish(status="error", summary=msg)
        return 1

    morning_count = sum(1 for i in all_items if i.source == "morning")
    evening_count = sum(1 for i in all_items if i.source == "evening")

    if args.mode == "AM" and morning_count == 0:
        if discovered_counts["morning"] > 0:
            msg = f"morning briefing files were found for {run_date} but parsed 0 valid stories"
        else:
            msg = f"missing morning briefing source file for {run_date}"
        print(f"  ERROR: {msg}")
        print(f"NOTEBOOKLM_AM_ERROR: {msg}")
        run_logger.finish(status="error", summary=msg)
        return 2

    if args.mode == "PM" and evening_count == 0:
        if discovered_counts["evening"] > 0:
            msg = f"evening briefing files were found for {run_date} but parsed 0 valid stories"
        else:
            msg = f"missing evening briefing source file for {run_date}"
        print(f"  ERROR: {msg}")
        print(f"NOTEBOOKLM_PM_ERROR: {msg}")
        run_logger.finish(status="error", summary=msg)
        return 2

    pipeline_cfg = dict(config.get("pipeline", {}))
    raw_capture_cfg = config.get("raw_capture", {})
    pipeline_cfg["raw_capture_root"] = raw_capture_cfg.get("output_root", str(_DEFAULT_RAW_CAPTURE_ROOT))
    recent_items = get_items_in_date_window(
        run_date,
        lookback_days=int(pipeline_cfg.get("redundancy_lookback_days", 7) or 0),
    )
    with run_logger.step("build_packs", item_count=len(all_items), recent_item_count=len(recent_items)):
        am_items, pm_items, am_path, pm_path = build_packs(
            all_items,
            run_date,
            pipeline_cfg,
            write_mode=args.mode,
            recent_items=recent_items,
        )
        run_logger.event(
            "packs_built",
            status="ok",
            am_count=len(am_items),
            pm_count=len(pm_items),
            am_path=str(am_path),
            pm_path=str(pm_path),
        )

    selected_pack_counts = {
        "AM": len(am_items),
        "PM": len(pm_items),
    }
    if args.mode in ("AM", "PM") and selected_pack_counts[args.mode] == 0:
        msg = f"{args.mode} pack built with 0 publishable items for {run_date}"
        print(f"  ERROR: {msg}")
        print(f"NOTEBOOKLM_{args.mode}_ERROR: {msg}")
        run_logger.finish(status="error", summary=msg)
        return 3

    selected_items = []
    if args.mode in ("ALL", "AM"):
        selected_items.extend(am_items)
    if args.mode in ("ALL", "PM"):
        selected_items.extend(pm_items)

    for item in selected_items:
        if item.stream:
            update_item_stream(item.item_id, item.stream)
    if args.mode in ("ALL", "AM"):
        for item in am_items:
            assign_pack(item.item_id, "AM")
        log_pack_run(run_date, "AM", len(am_items), str(am_path))
        print(f"  AM pack: {am_path.name}  ({len(am_items)} items)")
    if args.mode in ("ALL", "PM"):
        for item in pm_items:
            assign_pack(item.item_id, "PM")
        log_pack_run(run_date, "PM", len(pm_items), str(pm_path))
        print(f"  PM pack: {pm_path.name}  ({len(pm_items)} items)")

    if raw_capture_cfg.get("enabled", True):
        print("[4.5/5] Capturing raw article text...")
        raw_mode = args.mode
        with run_logger.step("raw_article_capture", mode=raw_mode):
            manifest = capture_raw_run(
                run_date,
                raw_mode,
                Path(raw_capture_cfg.get("output_root", str(_DEFAULT_RAW_CAPTURE_ROOT))),
                pipeline_cfg,
                run_logger=run_logger,
            )
        counts = manifest.get("counts", {})
        print(
            "  Raw capture: "
            f"{counts.get('captured', 0)} captured, "
            f"{counts.get('failed', 0)} failed, "
            f"{counts.get('no_url', 0)} no-url"
        )
        if counts.get("failed", 0):
            run_logger.event(
                "raw_capture_partial_failure",
                status="warning",
                failed=counts.get("failed", 0),
                captured=counts.get("captured", 0),
                no_url=counts.get("no_url", 0),
                manifest_path=manifest.get("kb_handoff", {}).get("manifest_path"),
            )

        # Rebuild packs after raw capture so the uploaded NotebookLM source contains the raw extracts.
        with run_logger.step("rebuild_packs_after_raw_capture", item_count=len(all_items)):
            am_items, pm_items, am_path, pm_path = build_packs(
                all_items,
                run_date,
                pipeline_cfg,
                write_mode=args.mode,
                recent_items=recent_items,
            )
            run_logger.event(
                "packs_rebuilt_after_raw_capture",
                status="ok",
                am_count=len(am_items),
                pm_count=len(pm_items),
                am_path=str(am_path),
                pm_path=str(pm_path),
            )

    print("[5/5] Publishing to NotebookLM...")
    nlm_cfg = config.get("notebooklm", {})
    adapter = NotebookLMAdapter(nlm_cfg, run_logger=run_logger)

    results: list[tuple[str, bool, str | None, str, str]] = []
    with run_logger.step("publish_to_notebooklm"):
        if args.mode in ("ALL", "AM"):
            results.append(("AM", *_publish_one(adapter, "AM", am_path, run_date)))
        if args.mode in ("ALL", "PM"):
            results.append(("PM", *_publish_one(adapter, "PM", pm_path, run_date)))
        run_logger.event(
            "notebooklm_publish_results",
            status="ok" if all(r[1] for r in results) else "error",
            results=[{"pack": label, "ok": ok, "url": url, "message": msg, "audio_status": audio_status} for label, ok, url, msg, audio_status in results],
        )

    for label, ok, url, _msg, audio_status in results:
        if ok and url and audio_status == "requested":
            with run_logger.boundary("smtp", "send_audio_link_email", pack_type=label, notebook_url=url):
                _notify_audio_link(run_date, label, url, config)

    print()
    print("=== Done ===")
    print("Outputs:")
    if args.mode in ("ALL", "AM"):
        print(f"  {am_path}")
        print(f"  {am_path.with_suffix('.json')}")
    if args.mode in ("ALL", "PM"):
        print(f"  {pm_path}")
        print(f"  {pm_path.with_suffix('.json')}")

    print("Publish results:")
    for label, ok, url, msg, _audio_status in results:
        status = "OK" if ok else "ERROR"
        print(f"  {label}: {status}")
        if url:
            print(f"    {url}")
        print(msg)

    print()
    run_logger.finish(
        status="ok" if all(ok for _label, ok, _url, _msg, _audio_status in results) else "error",
        summary="; ".join(msg for _label, _ok, _url, msg, _audio_status in results),
    )
    return 0 if all(ok for _label, ok, _url, _msg, _audio_status in results) else 4


if __name__ == "__main__":
    sys.exit(main())

