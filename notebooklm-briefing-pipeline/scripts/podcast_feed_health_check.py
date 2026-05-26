"""Self-consistency check and recovery for the public Apple Podcasts feed.

Checks recent successful NotebookLM publishes against the live RSS feed:
- if NotebookLM audio is not ready yet, log a deferred/non-fatal event;
- if audio is ready but the live feed is missing the episode, run the R2 sync;
- verify the live feed item and MP3 enclosure are externally reachable.

This script is intentionally safe to run from Scheduled Tasks after every sync.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
EVENT_LOG = ROOT / "logs" / "podcast_feed_events.jsonl"
SYNC_SCRIPT = ROOT / "scripts" / "sync_podcast_feed_to_r2.py"
PUBLIC_FEED_URL = "https://podcast.example.com/feed.xml"

sys.path.insert(0, str(ROOT))

from podcast_bridge import ensure_mp3_cached, load_config, load_feed_episodes  # noqa: E402
from pipeline.notebooklm_adapter import NotebookLMAdapter  # noqa: E402


def emit_event(step: str, status: str, **fields) -> None:
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "component": "podcast_feed_health_check",
        "step": step,
        "status": status,
        **fields,
    }
    line = json.dumps(payload, ensure_ascii=False, default=str)
    print(f"EVENT {line}", flush=True)
    with EVENT_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch_text(url: str, timeout: int = 30) -> str:
    req = Request(url, headers={"User-Agent": "OpenClaw-PodcastHealthCheck/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        if status < 200 or status >= 300:
            raise RuntimeError(f"HTTP {status} for {url}")
        return resp.read().decode("utf-8", errors="replace")


def head_ok(url: str, timeout: int = 30) -> tuple[bool, str]:
    req = Request(url, method="HEAD", headers={"User-Agent": "OpenClaw-PodcastHealthCheck/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            return 200 <= status < 300, f"HTTP {status}"
    except HTTPError as exc:
        if exc.code in (403, 405, 501):
            return range_get_ok(url, timeout=timeout)
        return False, f"HTTP {exc.code}"
    except URLError as exc:
        return False, str(exc.reason)
    except Exception as exc:
        return False, str(exc)


def range_get_ok(url: str, timeout: int = 30) -> tuple[bool, str]:
    req = Request(
        url,
        headers={
            "User-Agent": "OpenClaw-PodcastHealthCheck/1.0",
            "Range": "bytes=0-0",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            return 200 <= status < 300, f"HTTP {status} via range GET"
    except HTTPError as exc:
        return False, f"HTTP {exc.code} via range GET"
    except URLError as exc:
        return False, f"{exc.reason} via range GET"
    except Exception as exc:
        return False, f"{exc} via range GET"


def run_sync(recent: int) -> None:
    emit_event("recovery_sync", "started", recent=recent)
    subprocess.run([sys.executable, str(SYNC_SCRIPT), "--recent", str(recent)], cwd=str(ROOT.parent), check=True, timeout=1800)
    emit_event("recovery_sync", "ok", recent=recent)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recent", type=int, default=20)
    parser.add_argument("--check", type=int, default=6, help="Number of newest publish runs to verify")
    parser.add_argument("--recover", action="store_true", help="Run sync if ready audio is missing from live feed")
    parser.add_argument("--require-date", help="Require a specific run date to be public-feed ready, e.g. 2026-05-20")
    parser.add_argument("--require-mode", choices=["AM", "PM", "RESEARCH"], help="Require a specific pack type to be public-feed ready")
    args = parser.parse_args()

    if bool(args.require_date) != bool(args.require_mode):
        parser.error("--require-date and --require-mode must be supplied together")

    required_guid_prefix = f"{args.require_date}:{args.require_mode}:" if args.require_date and args.require_mode else None

    emit_event("health_check", "started", recent=args.recent, check=args.check, recover=args.recover, require_date=args.require_date, require_mode=args.require_mode)
    config = load_config()
    bridge_cfg = config.get("podcast_bridge", {})
    cache_dir = Path(bridge_cfg.get("cache_dir") or ROOT / "podcast_cache")
    adapter = NotebookLMAdapter(config.get("notebooklm", {}))

    candidates = load_feed_episodes(limit=args.recent)
    episodes = candidates[: args.check]
    if required_guid_prefix and not any(ep.guid.startswith(required_guid_prefix) for ep in episodes):
        required = next((ep for ep in candidates if ep.guid.startswith(required_guid_prefix)), None)
        if required:
            episodes = [required] + episodes
        else:
            emit_event("required_episode", "missing", guid_prefix=required_guid_prefix)
            return 4
    emit_event("load_recent_publishes", "ok", count=len(episodes))

    try:
        feed_xml = fetch_text(PUBLIC_FEED_URL)
        emit_event("fetch_live_feed", "ok", bytes=len(feed_xml.encode("utf-8")), items=feed_xml.count("<item>"))
    except Exception as exc:
        emit_event("fetch_live_feed", "failed", error=str(exc))
        if args.recover:
            run_sync(args.recent)
            feed_xml = fetch_text(PUBLIC_FEED_URL)
            emit_event("fetch_live_feed_after_recovery", "ok", bytes=len(feed_xml.encode("utf-8")), items=feed_xml.count("<item>"))
        else:
            return 2

    failures: list[str] = []
    pending_required: list[str] = []
    recovery_needed = False
    for ep in episodes:
        emit_event("verify_episode", "started", guid=ep.guid, notebook_id=ep.notebook_id)
        try:
            mp3_path = ensure_mp3_cached(adapter, cache_dir, ep, config)
            emit_event("verify_audio_ready", "ok", guid=ep.guid, notebook_id=ep.notebook_id, bytes=mp3_path.stat().st_size)
        except Exception as exc:
            # NotebookLM can take time to render audio. This is non-fatal for
            # background sweeps, but a post-publish required episode must not be
            # reported as feed-ready while audio is still absent.
            emit_event("verify_audio_ready", "deferred", guid=ep.guid, notebook_id=ep.notebook_id, error=str(exc))
            if required_guid_prefix and ep.guid.startswith(required_guid_prefix):
                pending_required.append(f"audio not ready {ep.guid}: {exc}")
            continue

        expected_audio_url = f"https://podcast.example.com/audio/{ep.notebook_id}.mp3"
        in_feed = ep.guid in feed_xml and expected_audio_url in feed_xml
        if not in_feed:
            emit_event("verify_episode_in_feed", "failed", guid=ep.guid, notebook_id=ep.notebook_id, audio_url=expected_audio_url)
            recovery_needed = True
            failures.append(f"missing feed item {ep.guid}")
        else:
            emit_event("verify_episode_in_feed", "ok", guid=ep.guid, notebook_id=ep.notebook_id)

        ok, detail = head_ok(expected_audio_url)
        if ok:
            emit_event("verify_public_audio", "ok", guid=ep.guid, notebook_id=ep.notebook_id, audio_url=expected_audio_url, detail=detail)
        else:
            emit_event("verify_public_audio", "failed", guid=ep.guid, notebook_id=ep.notebook_id, audio_url=expected_audio_url, detail=detail)
            recovery_needed = True
            failures.append(f"unreachable audio {ep.guid}: {detail}")

    if pending_required:
        emit_event("health_check", "pending", pending=pending_required)
        return 4

    if recovery_needed and args.recover:
        run_sync(args.recent)
        feed_xml = fetch_text(PUBLIC_FEED_URL)
        emit_event("fetch_live_feed_after_recovery", "ok", bytes=len(feed_xml.encode("utf-8")), items=feed_xml.count("<item>"))
        remaining: list[str] = []
        for ep in episodes:
            expected_audio_url = f"https://podcast.example.com/audio/{ep.notebook_id}.mp3"
            try:
                ensure_mp3_cached(adapter, cache_dir, ep, config)
            except Exception:
                continue
            if ep.guid not in feed_xml or expected_audio_url not in feed_xml:
                remaining.append(ep.guid)
                continue
            ok, detail = head_ok(expected_audio_url)
            if not ok:
                remaining.append(f"{ep.guid} public audio unreachable after recovery: {detail}")
        if remaining:
            emit_event("health_check", "failed", remaining=remaining)
            return 3
        if required_guid_prefix and not any(ep.guid.startswith(required_guid_prefix) for ep in episodes):
            emit_event("required_episode", "missing_after_recovery", guid_prefix=required_guid_prefix)
            return 4
        emit_event("health_check", "recovered", recovered_failures=failures)
        return 0

    if failures:
        emit_event("health_check", "failed", failures=failures)
        return 1

    emit_event("health_check", "ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

