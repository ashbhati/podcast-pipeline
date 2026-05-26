"""Sync the NotebookLM podcast feed and ready MP3 audio to Cloudflare R2.

This is the durable Apple Podcasts publishing step:
1. find recent published NotebookLM runs from state.db,
2. download ready NotebookLM audio and transcode to MP3,
3. upload MP3 objects to R2 under audio/<notebook_id>.mp3,
4. generate the RSS feed with only ready MP3 episodes,
5. upload podcast.xml/feed.xml/rss.xml to R2 for the Cloudflare Worker to serve.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
WEB_FEED_MODULE = WORKSPACE / "your-static-site" / "content" / "podcastFeedXml.ts"
TMP_FEED = ROOT / "outputs" / "podcast.xml"
PODCAST_COVER = ROOT / "podcast_assets" / "podcast-cover.jpg"
EVENT_LOG = ROOT / "logs" / "podcast_feed_events.jsonl"

sys.path.insert(0, str(ROOT))

from podcast_bridge import (  # noqa: E402
    build_rss_xml,
    ensure_mp3_cached,
    load_config,
    load_feed_episodes,
)
from pipeline.notebooklm_adapter import NotebookLMAdapter  # noqa: E402


def emit_event(step: str, status: str, **fields) -> None:
    """Append a machine-readable success/failure event for early diagnosis."""
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "component": "podcast_feed_r2_sync",
        "step": step,
        "status": status,
        **fields,
    }
    line = json.dumps(payload, ensure_ascii=False, default=str)
    print(f"EVENT {line}", flush=True)
    with EVENT_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd: list[str], *, dry_run: bool = False, timeout: int = 300) -> None:
    print("$ " + " ".join(cmd), flush=True)
    if dry_run:
        return
    env = os.environ.copy()
    # Scheduled Tasks run non-interactively. Make npm/npx fail/continue without
    # prompting for package install confirmation, which previously wedged sync.
    env.setdefault("CI", "true")
    env.setdefault("npm_config_yes", "true")
    subprocess.run(cmd, check=True, cwd=str(WORKSPACE), timeout=timeout, env=env)


def powershell_escape_single(value: str) -> str:
    return value.replace("'", "''")


def upload_r2(key: str, file_path: Path, content_type: str, *, dry_run: bool = False) -> None:
    npx = "npx.cmd" if os.name == "nt" else "npx"
    run(
        [
            npx,
            "--yes",
            "wrangler",
            "r2",
            "object",
            "put",
            f"your-podcast-audio-bucket/{key}",
            "--file",
            str(file_path),
            "--content-type",
            content_type,
            "--remote",
        ],
        dry_run=dry_run,
        timeout=600,
    )


def delete_r2(key: str, *, dry_run: bool = False) -> None:
    npx = "npx.cmd" if os.name == "nt" else "npx"
    run(
        [
            npx,
            "--yes",
            "wrangler",
            "r2",
            "object",
            "delete",
            f"your-podcast-audio-bucket/{key}",
            "--remote",
        ],
        dry_run=dry_run,
        timeout=120,
    )


def parse_cached_mp3(file_path: Path) -> dict | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})_(AM|PM|RESEARCH)_(.+)\.mp3$", file_path.name)
    if not match:
        return None
    return {
        "path": file_path,
        "run_date": match.group(1),
        "pack_type": match.group(2),
        "notebook_id": match.group(3),
        "key": f"audio/{match.group(3)}.mp3",
        "size": file_path.stat().st_size,
        "mtime": file_path.stat().st_mtime,
    }


def prune_r2_audio_fifo(cache_dir: Path, *, max_gb: float, dry_run: bool = False) -> tuple[int, int, int]:
    """Keep newest cached MP3s under max_gb and delete older R2 objects.

    The sync pipeline is the only writer for podcast audio objects, and every
    upload comes from this cache. Keeping the cache and R2 bucket pruned by the
    same FIFO plan prevents the public feed from referencing deleted objects.
    """
    limit_bytes = int(max_gb * 1024 * 1024 * 1024)
    files = []
    if cache_dir.exists():
        for path in cache_dir.glob("*.mp3"):
            if path.stat().st_size <= 0:
                continue
            item = parse_cached_mp3(path)
            if item:
                files.append(item)

    files.sort(key=lambda item: (item["run_date"], item["mtime"], item["path"].name), reverse=True)
    kept: list[dict] = []
    dropped: list[dict] = []
    kept_bytes = 0
    for item in files:
        if kept_bytes + item["size"] <= limit_bytes:
            kept.append(item)
            kept_bytes += item["size"]
        else:
            dropped.append(item)

    emit_event(
        "r2_audio_fifo_plan",
        "ok",
        max_gb=max_gb,
        limit_bytes=limit_bytes,
        cache_files=len(files),
        keep_count=len(kept),
        keep_bytes=kept_bytes,
        drop_count=len(dropped),
        drop_bytes=sum(item["size"] for item in dropped),
        dry_run=dry_run,
    )

    deleted = 0
    for item in sorted(dropped, key=lambda item: (item["run_date"], item["mtime"], item["path"].name)):
        emit_event("delete_old_r2_audio", "started", key=item["key"], bytes=item["size"], dry_run=dry_run)
        delete_r2(item["key"], dry_run=dry_run)
        if not dry_run:
            item["path"].unlink(missing_ok=True)
        deleted += 1
        emit_event("delete_old_r2_audio", "ok", key=item["key"], bytes=item["size"], dry_run=dry_run)

    return len(kept), kept_bytes, deleted


def write_feed_module(xml: str, *, dry_run: bool = False) -> None:
    content = "export const PODCAST_FEED_XML = String.raw`" + xml.replace("`", "\\`") + "`\n"
    if dry_run:
        print(f"DRY would write {WEB_FEED_MODULE}")
        return
    WEB_FEED_MODULE.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recent", type=int, default=20, help="Recent publish runs to test/cache")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-web-module", action="store_true", help="Do not update your-static-site fallback feed module")
    parser.add_argument("--keep-legacy-aliases", action="store_true", help="Do not delete legacy podcast.xml/rss.xml R2 objects")
    parser.add_argument("--allow-empty-feed", action="store_true", help="Allow uploading a feed with zero ready MP3 episodes")
    parser.add_argument("--disable-r2-prune", action="store_true", help="Disable FIFO R2 audio pruning")
    parser.add_argument("--r2-max-gb", type=float, help="FIFO audio retention cap in GB; defaults to podcast_bridge.r2_audio_retention_max_gb or 8")
    args = parser.parse_args()

    emit_event("sync", "started", recent=args.recent, dry_run=args.dry_run)
    try:
        config = load_config()
        emit_event("load_config", "ok")
    except Exception as exc:
        emit_event("load_config", "failed", error=str(exc))
        raise
    bridge_cfg = config.setdefault("podcast_bridge", {})
    cache_dir = Path(bridge_cfg.get("cache_dir") or ROOT / "podcast_cache")
    r2_retention_enabled = bool(bridge_cfg.get("r2_audio_retention_enabled", True)) and not args.disable_r2_prune
    r2_max_gb = float(args.r2_max_gb if args.r2_max_gb is not None else bridge_cfg.get("r2_audio_retention_max_gb", 8.0))
    adapter = NotebookLMAdapter(config.get("notebooklm", {}))

    ready_mp3s: list[tuple[str, Path]] = []
    episodes = load_feed_episodes(limit=args.recent)
    emit_event("load_feed_episodes", "ok", count=len(episodes))
    for ep in episodes:
        try:
            emit_event("cache_audio", "started", guid=ep.guid, notebook_id=ep.notebook_id)
            mp3_path = ensure_mp3_cached(adapter, cache_dir, ep, config)
        except Exception as exc:
            print(f"SKIP {ep.guid}: {exc}")
            emit_event("cache_audio", "skipped", guid=ep.guid, notebook_id=ep.notebook_id, error=str(exc))
            continue
        if mp3_path.exists() and mp3_path.stat().st_size > 0:
            ready_mp3s.append((ep.notebook_id, mp3_path))
            print(f"READY {ep.guid}: {mp3_path.name} {mp3_path.stat().st_size}")
            emit_event("cache_audio", "ok", guid=ep.guid, notebook_id=ep.notebook_id, bytes=mp3_path.stat().st_size)

    if episodes and not ready_mp3s and not args.allow_empty_feed:
        msg = (
            "refusing to upload an empty podcast feed because recent publishes exist "
            "but no MP3 audio could be cached"
        )
        emit_event("empty_feed_guard", "failed", episodes=len(episodes), error=msg)
        raise RuntimeError(msg)

    for notebook_id, mp3_path in ready_mp3s:
        key = f"audio/{notebook_id}.mp3"
        try:
            emit_event("upload_audio", "started", key=key, bytes=mp3_path.stat().st_size)
            upload_r2(key, mp3_path, "audio/mpeg", dry_run=args.dry_run)
            emit_event("upload_audio", "ok", key=key, bytes=mp3_path.stat().st_size)
        except Exception as exc:
            emit_event("upload_audio", "failed", key=key, error=str(exc))
            raise

    if r2_retention_enabled:
        try:
            kept_count, kept_bytes, deleted_count = prune_r2_audio_fifo(cache_dir, max_gb=r2_max_gb, dry_run=args.dry_run)
            emit_event(
                "r2_audio_fifo_cleanup",
                "ok",
                max_gb=r2_max_gb,
                kept_count=kept_count,
                kept_bytes=kept_bytes,
                deleted_count=deleted_count,
                dry_run=args.dry_run,
            )
        except Exception as exc:
            emit_event("r2_audio_fifo_cleanup", "failed", max_gb=r2_max_gb, error=str(exc), dry_run=args.dry_run)
            raise
    else:
        emit_event("r2_audio_fifo_cleanup", "skipped", reason="disabled")

    feed_config = copy.deepcopy(config)
    feed_bridge = feed_config.setdefault("podcast_bridge", {})
    # Feed is served by the Worker from R2; enclosures are served by the same Worker.
    feed_bridge["base_url"] = "https://podcast.example.com"
    feed_bridge["site_url"] = "https://example.com/podcast"
    feed_bridge["image_url"] = "https://podcast.example.com/podcast-cover.jpg"
    feed_bridge["prefer_mp3"] = True
    feed_bridge["exclude_unready_audio"] = True
    feed_bridge["warm_recent_items"] = 0

    emit_event("build_feed", "started")
    xml = build_rss_xml(feed_config)
    item_count = xml.count("<item>")
    emit_event("build_feed", "ok", items=item_count, bytes=len(xml.encode("utf-8")))
    TMP_FEED.parent.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        TMP_FEED.write_text(xml, encoding="utf-8", newline="\n")
        emit_event("write_local_feed", "ok", path=str(TMP_FEED))
    else:
        print(f"DRY would write {TMP_FEED}")

    # Preserve a Vercel/static fallback, but the canonical feed is the Worker/R2 copy.
    if not args.no_web_module:
        try:
            write_feed_module(xml, dry_run=args.dry_run)
            emit_event("write_web_fallback_feed", "ok", path=str(WEB_FEED_MODULE))
        except Exception as exc:
            emit_event("write_web_fallback_feed", "failed", path=str(WEB_FEED_MODULE), error=str(exc))
            raise

    if PODCAST_COVER.exists():
        emit_event("upload_cover", "started", key="podcast-cover.jpg")
        upload_r2("podcast-cover.jpg", PODCAST_COVER, "image/jpeg", dry_run=args.dry_run)
        emit_event("upload_cover", "ok", key="podcast-cover.jpg")
    else:
        print(f"WARN podcast cover not found: {PODCAST_COVER}", flush=True)
        emit_event("upload_cover", "skipped", error=f"not found: {PODCAST_COVER}")

    # Canonical public RSS path: https://podcast.example.com/feed.xml
    emit_event("upload_feed", "started", key="feed.xml", bytes=TMP_FEED.stat().st_size if TMP_FEED.exists() else 0)
    upload_r2("feed.xml", TMP_FEED, "application/rss+xml; charset=utf-8", dry_run=args.dry_run)
    emit_event("upload_feed", "ok", key="feed.xml", bytes=TMP_FEED.stat().st_size if TMP_FEED.exists() else 0)

    if not args.keep_legacy_aliases:
        # Avoid split-brain feeds: these legacy objects previously drifted independently.
        for key in ("podcast.xml", "rss.xml"):
            try:
                emit_event("delete_legacy_alias", "started", key=key)
                delete_r2(key, dry_run=args.dry_run)
                emit_event("delete_legacy_alias", "ok", key=key)
            except subprocess.CalledProcessError as exc:
                print(f"WARN failed to delete legacy {key}: {exc}", flush=True)
                emit_event("delete_legacy_alias", "failed", key=key, error=str(exc))

    print(f"Synced {len(ready_mp3s)} ready MP3s and canonical feed.xml with {item_count} items")
    emit_event("sync", "ok", ready_mp3s=len(ready_mp3s), items=item_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

