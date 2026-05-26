#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import wave
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse
from xml.sax.saxutils import escape

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from pipeline.db import get_latest_successful_publish_runs, init_db
from pipeline.notebooklm_adapter import NotebookLMAdapter

_CONFIG_PATH = _HERE / "config.json"
_DEFAULT_CACHE = _HERE / "podcast_cache"
_DEFAULT_ASSETS = _HERE / "podcast_assets"


@dataclass
class Episode:
    run_date: str
    pack_type: str
    notebook_url: str
    message: str
    output_path: str
    created_at: str
    notebook_id: str

    @property
    def guid(self) -> str:
        return f"{self.run_date}:{self.pack_type}:{self.notebook_id}"

    @property
    def audio_path_key(self) -> str:
        return f"{self.run_date}_{self.pack_type}_{self.notebook_id}.wav"

    @property
    def title(self) -> str:
        if self.pack_type == "AM":
            return f"AI Morning Briefing — {self.run_date}"
        if self.pack_type == "PM":
            return f"AI Evening Briefing — {self.run_date}"
        if self.pack_type == "RESEARCH":
            paper_name = load_research_paper_name(self.output_path)
            return f"Research Paper Deep Dive — {paper_name}" if paper_name else f"Research Paper Deep Dive — {self.run_date}"
        return f"{self.pack_type} — {self.run_date}"

    @property
    def audio_mp3_path_key(self) -> str:
        return f"{self.run_date}_{self.pack_type}_{self.notebook_id}.mp3"


@dataclass
class EpisodeAsset:
    file_path: Path | None
    size_bytes: int
    duration_seconds: int
    mime_type: str = "audio/wav"
    extension: str = "wav"


def _clean_story_title(title: str) -> str | None:
    t = " ".join((title or "").split())
    if not t:
        return None
    bad_prefixes = (
        "subject:",
        "## ",
        "1. clean up feed metadata",
        "- `/healthz`",
        "- builds rss items",
        "important caveat",
    )
    lowered = t.lower()
    if lowered.startswith(bad_prefixes):
        return None
    if len(t) < 12:
        return None
    if t.startswith("Title: "):
        t = t[7:].strip()
    if t.startswith("BRIEFING_READY:"):
        return None
    return t[:160]


def load_config() -> dict:
    if _CONFIG_PATH.exists():
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    return {}


def parse_notebook_id(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"/notebook/([^/?#]+)", url)
    return unquote(m.group(1)) if m else None


def load_feed_episodes(limit: int = 100) -> list[Episode]:
    init_db()
    rows = get_latest_successful_publish_runs(limit=limit)
    episodes: list[Episode] = []
    for row in rows:
        notebook_id = parse_notebook_id(row.get("notebook_url"))
        if not notebook_id:
            continue
        episodes.append(
            Episode(
                run_date=row["run_date"],
                pack_type=row["pack_type"],
                notebook_url=row["notebook_url"],
                message=row.get("message") or "",
                output_path=row.get("output_path") or "",
                created_at=row.get("created_at") or "",
                notebook_id=notebook_id,
            )
        )
    episodes.sort(key=lambda e: (e.run_date, e.pack_type), reverse=True)
    return episodes


def _read_markdown_lines(output_path: str) -> list[str]:
    md_path = Path(output_path)
    if not md_path.exists():
        return []
    try:
        return md_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []


def load_research_paper_name(output_path: str) -> str | None:
    for line in _read_markdown_lines(output_path):
        if line.startswith("## "):
            name = line[3:].strip()
            if name:
                return name
    return None


def load_research_paper_summary(output_path: str) -> str | None:
    lines = _read_markdown_lines(output_path)
    if not lines:
        return None

    def extract_section(section_name: str) -> str:
        in_section = False
        collected: list[str] = []
        for line in lines:
            if line.strip() == f"## {section_name}":
                in_section = True
                continue
            if in_section and line.startswith("## "):
                break
            if in_section:
                stripped = line.strip()
                if stripped:
                    collected.append(stripped)
        return " ".join(collected).strip()

    abstract = extract_section("Abstract")
    if abstract:
        return abstract

    snapshot = extract_section("Short trend snapshot")
    if snapshot:
        return snapshot

    paper_name = load_research_paper_name(output_path)
    return f"Technical audio overview for {paper_name}." if paper_name else None


def load_briefing_summary(output_path: str) -> tuple[str, list[str]]:
    md_path = Path(output_path)
    if "research-paper-audio-outputs" in str(md_path):
        return load_research_paper_summary(output_path) or "", []

    json_path = md_path.with_suffix(".json")
    if not json_path.exists():
        return "", []
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return "", []
    items = data.get("items") or []
    titles: list[str] = []
    for item in items:
        source = str(item.get("source") or "").strip().lower()
        cleaned = _clean_story_title(str(item.get("title", "")))
        if source == "manual":
            continue
        if cleaned:
            titles.append(cleaned)
    description = "; ".join(titles[:5])
    return description, titles[:5]


def get_episode_asset(adapter: NotebookLMAdapter, cache_dir: Path, episode: Episode, warm: bool = False) -> EpisodeAsset:
    file_path = cache_dir / episode.audio_path_key
    if file_path.exists() and file_path.stat().st_size > 0:
        return EpisodeAsset(
            file_path=file_path,
            size_bytes=file_path.stat().st_size,
            duration_seconds=get_wav_duration_seconds(file_path),
            mime_type="audio/wav",
            extension="wav",
        )
    if warm:
        try:
            resolved = ensure_audio_cached(adapter, cache_dir, episode)
            return EpisodeAsset(
                file_path=resolved,
                size_bytes=resolved.stat().st_size,
                duration_seconds=get_wav_duration_seconds(resolved),
                mime_type="audio/wav",
                extension="wav",
            )
        except Exception:
            pass
    return EpisodeAsset(file_path=None, size_bytes=0, duration_seconds=0)


def get_primary_episode_asset(adapter: NotebookLMAdapter, cache_dir: Path, episode: Episode, config: dict, warm: bool = False) -> EpisodeAsset:
    bridge_cfg = config.get("podcast_bridge", {})
    prefer_mp3 = bool(bridge_cfg.get("prefer_mp3", True))
    if prefer_mp3:
        mp3_path = cache_dir / episode.audio_mp3_path_key
        if mp3_path.exists() and mp3_path.stat().st_size > 0:
            wav_path = cache_dir / episode.audio_path_key
            duration = get_wav_duration_seconds(wav_path) if wav_path.exists() else 0
            return EpisodeAsset(
                file_path=mp3_path,
                size_bytes=mp3_path.stat().st_size,
                duration_seconds=duration,
                mime_type="audio/mpeg",
                extension="mp3",
            )
        if warm:
            try:
                resolved = ensure_mp3_cached(adapter, cache_dir, episode, config)
                wav_path = cache_dir / episode.audio_path_key
                duration = get_wav_duration_seconds(wav_path) if wav_path.exists() else 0
                return EpisodeAsset(
                    file_path=resolved,
                    size_bytes=resolved.stat().st_size,
                    duration_seconds=duration,
                    mime_type="audio/mpeg",
                    extension="mp3",
                )
            except Exception:
                pass
    return get_episode_asset(adapter, cache_dir, episode, warm=warm)


def get_wav_duration_seconds(file_path: Path) -> int:
    try:
        with closing(wave.open(str(file_path), "rb")) as wav_file:
            rate = wav_file.getframerate() or 1
            frames = wav_file.getnframes()
        return max(1, int(round(frames / rate)))
    except Exception:
        return 0


def find_episode(notebook_id: str, limit: int = 200) -> Episode | None:
    return next((e for e in load_feed_episodes(limit=limit) if e.notebook_id == notebook_id), None)


def get_ffmpeg_path(config: dict) -> str:
    configured = str(config.get("podcast_bridge", {}).get("ffmpeg_path") or "").strip()
    if configured and Path(configured).exists():
        return configured
    candidates = [
        r"C:\Users\you\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe",
        "ffmpeg",
    ]
    for candidate in candidates:
        if candidate == "ffmpeg":
            return candidate
        if Path(candidate).exists():
            return candidate
    return "ffmpeg"


def ensure_audio_cached(adapter: NotebookLMAdapter, cache_dir: Path, episode: Episode) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / episode.audio_path_key
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path

    client = adapter._load_client()  # reuse existing auth loader
    if client is None:
        raise RuntimeError("NotebookLM auth client unavailable")

    tmp_path = out_path.with_suffix(".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    result = client.download_audio(episode.notebook_id, str(tmp_path))
    if asyncio.iscoroutine(result):
        asyncio.run(result)
    if not tmp_path.exists() or tmp_path.stat().st_size == 0:
        raise RuntimeError("download_audio produced no file")
    tmp_path.replace(out_path)
    return out_path


def ensure_mp3_cached(adapter: NotebookLMAdapter, cache_dir: Path, episode: Episode, config: dict) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / episode.audio_mp3_path_key
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path

    wav_path = ensure_audio_cached(adapter, cache_dir, episode)
    ffmpeg_path = get_ffmpeg_path(config)
    tmp_path = out_path.with_suffix(".tmp.mp3")
    if tmp_path.exists():
        tmp_path.unlink()

    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        str(wav_path),
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(tmp_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
        detail = (result.stderr or result.stdout or "ffmpeg failed").strip()
        raise RuntimeError(f"mp3 transcode failed: {detail}")
    tmp_path.replace(out_path)
    return out_path


class RangeFileMixin:
    def send_file_with_range(self, file_path: Path, content_type: str, head_only: bool = False):
        total = file_path.stat().st_size
        range_header = self.headers.get("Range")
        start = 0
        end = total - 1
        status = HTTPStatus.OK

        if range_header and range_header.startswith("bytes="):
            spec = range_header.split("=", 1)[1].strip()
            if "," in spec:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            s, _, e = spec.partition("-")
            if s:
                start = int(s)
            if e:
                end = int(e)
            if start > end or start >= total:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return
            end = min(end, total - 1)
            status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition", f'inline; filename="{file_path.name}"')
        self.send_header("Cache-Control", "public, max-age=3600")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()

        if head_only:
            return

        with file_path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
                remaining -= len(chunk)


class PodcastBridgeHandler(RangeFileMixin, BaseHTTPRequestHandler):
    server_version = "NotebookLMPodcastBridge/0.1"

    def do_HEAD(self):
        self._dispatch(head_only=True)

    def do_GET(self):
        self._dispatch(head_only=False)

    def log_message(self, fmt, *args):
        return

    @property
    def cfg(self) -> dict:
        return self.server.cfg  # type: ignore[attr-defined]

    @property
    def adapter(self) -> NotebookLMAdapter:
        return self.server.adapter  # type: ignore[attr-defined]

    @property
    def cache_dir(self) -> Path:
        return self.server.cache_dir  # type: ignore[attr-defined]

    @property
    def assets_dir(self) -> Path:
        return self.server.assets_dir  # type: ignore[attr-defined]

    def _authorized(self) -> bool:
        token = str(self.cfg.get("token") or "").strip()
        if not token:
            return True
        qs = parse_qs(urlparse(self.path).query)
        return qs.get("token", [""])[0] == token

    def _dispatch(self, head_only: bool):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/healthz":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            if not head_only:
                self.wfile.write(b"ok")
            return

        if not self._authorized():
            self.send_error(HTTPStatus.UNAUTHORIZED, "missing or invalid token")
            return

        if path in ("/feed.xml", "/rss.xml"):
            xml = build_rss_xml(self.server.full_config)  # type: ignore[attr-defined]
            body = xml.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
            return

        if path in ("/ab-feed.xml", "/ab-test/feed.xml"):
            xml = build_ab_test_rss_xml(self.server.full_config)  # type: ignore[attr-defined]
            body = xml.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
            return

        if path.startswith("/static/"):
            relative = path.removeprefix("/static/")
            if not relative or ".." in relative or relative.startswith("/"):
                self.send_error(HTTPStatus.BAD_REQUEST, "invalid static path")
                return
            file_path = (self.assets_dir / relative).resolve()
            if not str(file_path).startswith(str(self.assets_dir.resolve())) or not file_path.exists() or not file_path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "static file not found")
                return
            content_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".svg": "image/svg+xml",
                ".txt": "text/plain; charset=utf-8",
            }.get(file_path.suffix.lower(), "application/octet-stream")
            self.send_file_with_range(file_path, content_type, head_only=head_only)
            return

        m = re.fullmatch(r"/audio/([a-f0-9\-]+)\.wav", path)
        if m:
            notebook_id = m.group(1)
            episode = find_episode(notebook_id)
            if not episode:
                self.send_error(HTTPStatus.NOT_FOUND, "episode not found")
                return
            try:
                file_path = ensure_audio_cached(self.adapter, self.cache_dir, episode)
            except Exception as exc:
                self.send_error(HTTPStatus.BAD_GATEWAY, f"audio resolution failed: {exc}")
                return
            self.send_file_with_range(file_path, "audio/wav", head_only=head_only)
            return

        m = re.fullmatch(r"/audio/([a-f0-9\-]+)\.mp3", path)
        if m:
            notebook_id = m.group(1)
            episode = find_episode(notebook_id)
            if not episode:
                self.send_error(HTTPStatus.NOT_FOUND, "episode not found")
                return
            try:
                file_path = ensure_mp3_cached(self.adapter, self.cache_dir, episode, self.server.full_config)  # type: ignore[attr-defined]
            except Exception as exc:
                self.send_error(HTTPStatus.BAD_GATEWAY, f"audio resolution failed: {exc}")
                return
            self.send_file_with_range(file_path, "audio/mpeg", head_only=head_only)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "not found")


def build_base_url(cfg: dict) -> str:
    base_url = str(cfg.get("base_url") or "").strip()
    if base_url:
        return base_url.rstrip("/")
    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 8788))
    return f"http://{host}:{port}"


def build_rss_xml(config: dict) -> str:
    bridge_cfg = config.get("podcast_bridge", {})
    base_url = build_base_url(bridge_cfg)
    token = str(bridge_cfg.get("token") or "").strip()
    token_qs = f"?token={token}" if token else ""
    title = str(bridge_cfg.get("title") or "Ashish's AI News Briefings")
    description = str(bridge_cfg.get("description") or "Morning and evening AI news briefings, plus research paper deep dives.")
    site_url = str(bridge_cfg.get("site_url") or (base_url + token_qs)).strip()
    author = str(bridge_cfg.get("author") or "Ashish AI Briefings")
    owner_name = str(bridge_cfg.get("owner_name") or author)
    owner_email = str(bridge_cfg.get("owner_email") or "")
    language = str(bridge_cfg.get("language") or "en-us")
    explicit = str(bridge_cfg.get("explicit") or "false").lower()
    category = str(bridge_cfg.get("category") or "Technology")
    subcategory = str(bridge_cfg.get("subcategory") or "Tech News")
    podcast_type = str(bridge_cfg.get("podcast_type") or "episodic")
    image_url = str(bridge_cfg.get("image_url") or f"{base_url}/static/cover.png{token_qs}").strip()
    adapter = NotebookLMAdapter(config.get("notebooklm", {}))
    cache_dir = Path(bridge_cfg.get("cache_dir") or _DEFAULT_CACHE)
    warm_recent = max(0, int(bridge_cfg.get("warm_recent_items", 0)))
    exclude_unready_audio = bool(bridge_cfg.get("exclude_unready_audio", True))

    items_xml: list[str] = []
    episodes = load_feed_episodes(limit=int(bridge_cfg.get("max_items", 100)))
    for idx, ep in enumerate(episodes):
        summary, top_titles = load_briefing_summary(ep.output_path)
        if not summary:
            summary = ep.message or ep.notebook_url
        asset = get_primary_episode_asset(adapter, cache_dir, ep, config, warm=idx < warm_recent)
        if exclude_unready_audio and asset.size_bytes <= 0:
            continue
        enclosure_url = f"{base_url}/audio/{ep.notebook_id}.{asset.extension}{token_qs}"
        created = datetime.strptime(ep.created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if ep.created_at else datetime.now(timezone.utc)
        desc_html = escape(summary)
        if top_titles:
            desc_html += "<br/><br/>Top stories: " + escape("; ".join(top_titles))
        if ep.pack_type != "RESEARCH":
            desc_html += "<br/><br/>NotebookLM: " + escape(ep.notebook_url)
        duration_xml = f"      <itunes:duration>{asset.duration_seconds}</itunes:duration>" if asset.duration_seconds else None
        items_xml.append(
            "\n".join([
                "    <item>",
                f"      <title>{escape(ep.title)}</title>",
                f"      <guid isPermaLink=\"false\">{escape(ep.guid)}</guid>",
                f"      <pubDate>{format_datetime(created)}</pubDate>",
                f"      <description>{desc_html}</description>",
                f"      <itunes:summary>{escape(summary)}</itunes:summary>",
                f"      <itunes:author>{escape(author)}</itunes:author>",
                f"      <itunes:explicit>{escape(explicit)}</itunes:explicit>",
                f"      <link>{escape(ep.notebook_url)}</link>",
                f"      <enclosure url=\"{escape(enclosure_url)}\" type=\"{asset.mime_type}\" length=\"{asset.size_bytes}\" />",
                *([duration_xml] if duration_xml else []),
                "    </item>",
            ])
        )

    image_block = f"\n    <image><url>{escape(image_url)}</url><title>{escape(title)}</title><link>{escape(site_url)}</link></image>\n    <itunes:image href=\"{escape(image_url)}\" />" if image_url else ""
    owner_block = ""
    if owner_email:
        owner_block = "\n".join([
            "    <itunes:owner>",
            f"      <itunes:name>{escape(owner_name)}</itunes:name>",
            f"      <itunes:email>{escape(owner_email)}</itunes:email>",
            "    </itunes:owner>",
        ])

    xml = "\n".join([
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
        "<rss version=\"2.0\" xmlns:itunes=\"http://www.itunes.com/dtds/podcast-1.0.dtd\" xmlns:content=\"http://purl.org/rss/1.0/modules/content/\">",
        "  <channel>",
        f"    <title>{escape(title)}</title>",
        f"    <link>{escape(site_url)}</link>",
        f"    <description>{escape(description)}</description>",
        f"    <language>{escape(language)}</language>",
        f"    <itunes:author>{escape(author)}</itunes:author>",
        f"    <itunes:summary>{escape(description)}</itunes:summary>",
        f"    <itunes:explicit>{escape(explicit)}</itunes:explicit>",
        f"    <itunes:type>{escape(podcast_type)}</itunes:type>",
        f"    <itunes:category text=\"{escape(category)}\"><itunes:category text=\"{escape(subcategory)}\" /></itunes:category>",
        owner_block,
        f"    <lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>" + image_block,
        *items_xml,
        "  </channel>",
        "</rss>",
        "",
    ])
    return xml


def build_ab_test_rss_xml(config: dict) -> str:
    bridge_cfg = config.get("podcast_bridge", {})
    notebook_id = str(bridge_cfg.get("ab_test_notebook_id") or "").strip()
    if not notebook_id:
        return "<?xml version=\"1.0\" encoding=\"UTF-8\"?><rss version=\"2.0\"><channel><title>AB Test Missing</title><description>No ab_test_notebook_id configured.</description></channel></rss>"

    episode = find_episode(notebook_id)
    if not episode:
        return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><rss version=\"2.0\"><channel><title>AB Test Missing</title><description>Episode not found for {escape(notebook_id)}.</description></channel></rss>"

    base_url = build_base_url(bridge_cfg)
    token = str(bridge_cfg.get("token") or "").strip()
    token_qs = f"?token={token}" if token else ""
    site_url = str(bridge_cfg.get("site_url") or (base_url + token_qs)).strip()
    image_url = str(bridge_cfg.get("image_url") or f"{base_url}/static/cover.png{token_qs}").strip()
    author = str(bridge_cfg.get("author") or "Ashish AI Briefings")
    explicit = str(bridge_cfg.get("explicit") or "false").lower()
    owner_name = str(bridge_cfg.get("owner_name") or author)
    owner_email = str(bridge_cfg.get("owner_email") or "")
    adapter = NotebookLMAdapter(config.get("notebooklm", {}))
    cache_dir = Path(bridge_cfg.get("cache_dir") or _DEFAULT_CACHE)
    wav_path = ensure_audio_cached(adapter, cache_dir, episode)
    mp3_path = ensure_mp3_cached(adapter, cache_dir, episode, config)
    summary, top_titles = load_briefing_summary(episode.output_path)
    if not summary:
        summary = episode.message or episode.notebook_url
    if top_titles:
        summary += " | Top stories: " + "; ".join(top_titles)
    created = datetime.strptime(episode.created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if episode.created_at else datetime.now(timezone.utc)
    wav_url = f"{base_url}/audio/{episode.notebook_id}.wav{token_qs}"
    mp3_url = f"{base_url}/audio/{episode.notebook_id}.mp3{token_qs}"

    items = []
    for label, suffix, url, mime, file_path in [
        ("WAV", "wav", wav_url, "audio/wav", wav_path),
        ("MP3", "mp3", mp3_url, "audio/mpeg", mp3_path),
    ]:
        items.append("\n".join([
            "    <item>",
            f"      <title>{escape(episode.title)} [{label} test]</title>",
            f"      <guid isPermaLink=\"false\">{escape(episode.guid + ':' + suffix)}</guid>",
            f"      <pubDate>{format_datetime(created)}</pubDate>",
            f"      <description>{escape(summary)}</description>",
            f"      <itunes:summary>{escape(summary)}</itunes:summary>",
            f"      <itunes:author>{escape(author)}</itunes:author>",
            f"      <itunes:explicit>{escape(explicit)}</itunes:explicit>",
            f"      <link>{escape(episode.notebook_url)}</link>",
            f"      <enclosure url=\"{escape(url)}\" type=\"{mime}\" length=\"{file_path.stat().st_size}\" />",
            f"      <itunes:duration>{get_wav_duration_seconds(wav_path)}</itunes:duration>",
            "    </item>",
        ]))

    owner_block = ""
    if owner_email:
        owner_block = "\n".join([
            "    <itunes:owner>",
            f"      <itunes:name>{escape(owner_name)}</itunes:name>",
            f"      <itunes:email>{escape(owner_email)}</itunes:email>",
            "    </itunes:owner>",
        ])

    return "\n".join([
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
        "<rss version=\"2.0\" xmlns:itunes=\"http://www.itunes.com/dtds/podcast-1.0.dtd\" xmlns:content=\"http://purl.org/rss/1.0/modules/content/\">",
        "  <channel>",
        f"    <title>{escape(str(bridge_cfg.get('title') or 'Ashish AI Briefings'))} — A/B Test</title>",
        f"    <link>{escape(site_url)}</link>",
        "    <description>WAV vs MP3 playback test for a single episode.</description>",
        "    <language>en-us</language>",
        f"    <itunes:author>{escape(author)}</itunes:author>",
        "    <itunes:summary>WAV vs MP3 playback test for a single episode.</itunes:summary>",
        f"    <itunes:explicit>{escape(explicit)}</itunes:explicit>",
        "    <itunes:type>episodic</itunes:type>",
        owner_block,
        f"    <lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>",
        f"    <image><url>{escape(image_url)}</url><title>A/B Test</title><link>{escape(site_url)}</link></image>",
        f"    <itunes:image href=\"{escape(image_url)}\" />",
        *items,
        "  </channel>",
        "</rss>",
        "",
    ])


def make_server(config: dict) -> ThreadingHTTPServer:
    bridge_cfg = config.get("podcast_bridge", {})
    host = bridge_cfg.get("host", "127.0.0.1")
    port = int(bridge_cfg.get("port", 8788))
    cache_dir = Path(bridge_cfg.get("cache_dir") or _DEFAULT_CACHE)
    assets_dir = Path(bridge_cfg.get("assets_dir") or _DEFAULT_ASSETS)
    adapter = NotebookLMAdapter(config.get("notebooklm", {}))
    httpd = ThreadingHTTPServer((host, port), PodcastBridgeHandler)
    httpd.cfg = bridge_cfg  # type: ignore[attr-defined]
    httpd.full_config = config  # type: ignore[attr-defined]
    httpd.adapter = adapter  # type: ignore[attr-defined]
    httpd.cache_dir = cache_dir  # type: ignore[attr-defined]
    httpd.assets_dir = assets_dir  # type: ignore[attr-defined]
    return httpd


def main() -> int:
    parser = argparse.ArgumentParser(description="NotebookLM podcast bridge (RSS + audio resolver)")
    parser.add_argument("serve", nargs="?", default="serve")
    parser.add_argument("--print-feed", action="store_true", help="Print RSS XML and exit")
    parser.add_argument("--prewarm", type=int, default=0, help="Download/cache the most recent N episode audios and exit")
    args = parser.parse_args()

    config = load_config()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.print_feed:
        print(build_rss_xml(config))
        return 0

    if args.prewarm > 0:
        bridge_cfg = config.get("podcast_bridge", {})
        cache_dir = Path(bridge_cfg.get("cache_dir") or _DEFAULT_CACHE)
        adapter = NotebookLMAdapter(config.get("notebooklm", {}))
        episodes = load_feed_episodes(limit=args.prewarm)
        for ep in episodes:
            try:
                file_path = ensure_audio_cached(adapter, cache_dir, ep)
                print(f"OK {ep.guid} {file_path.stat().st_size} {file_path}")
            except Exception as exc:
                print(f"SKIP {ep.guid} {exc}")
        return 0

    server = make_server(config)
    bridge_cfg = config.get("podcast_bridge", {})
    print(f"Podcast bridge listening on {build_base_url(bridge_cfg)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

