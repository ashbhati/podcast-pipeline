#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from pipeline.db import get_items_for_date, init_db
from pipeline.pack_builder import build_packs

DEFAULT_VAULT_RAW = Path(r"C:\Users\you\Projects\SecondBrain\raw\news")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0 Safari/537.36"
)


def slugify(text: str, limit: int = 90) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text[:limit] or "article").strip("-")


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return None


def is_capture_candidate(item) -> bool:
    if not item.url:
        return False
    domain = domain_of(item.url) or ""
    blocked_domains = {
        "notebooklm.google.com",
        "discord.com",
        "www.discord.com",
    }
    if domain in blocked_domains:
        return False
    title = (item.title or "").strip().lower()
    blocked_prefixes = (
        "notebooklm_",
        "email_sent:",
        "email_error:",
    )
    if any(title.startswith(p) for p in blocked_prefixes):
        return False
    return item.url.startswith("http://") or item.url.startswith("https://")


def fetch_html(url: str, timeout: int = 25) -> str:
    r = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        allow_redirects=True,
    )
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text


def clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def pick_best_container(soup: BeautifulSoup):
    selectors = [
        "article",
        "main",
        "[role='main']",
        ".article-body",
        ".post-content",
        ".entry-content",
        ".article__body",
        ".story-body",
        ".article-content",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node and len(node.get_text(" ", strip=True)) > 500:
            return node

    candidates = []
    for node in soup.find_all(["div", "section"]):
        paragraphs = node.find_all("p")
        if len(paragraphs) < 3:
            continue
        score = sum(len(p.get_text(" ", strip=True)) for p in paragraphs)
        if score > 800:
            candidates.append((score, node))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    return soup.body or soup


def extract_article(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form", "button", "aside", "nav", "footer"]):
        tag.decompose()

    title = None
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()

    container = pick_best_container(soup)

    paragraphs = []
    headings = []
    for el in container.find_all(["h2", "h3", "p", "li"]):
        txt = el.get_text(" ", strip=True)
        if not txt or len(txt) < 20:
            continue
        if el.name in ("h2", "h3"):
            headings.append(txt)
            paragraphs.append(f"## {txt}")
        else:
            paragraphs.append(txt)

    text = clean_text("\n\n".join(paragraphs))
    if len(text) < 500:
        # fallback: all visible paragraphs in document
        paras = []
        for p in soup.find_all(["p", "li"]):
            txt = p.get_text(" ", strip=True)
            if len(txt) >= 30:
                paras.append(txt)
        text = clean_text("\n\n".join(paras))

    if len(text) < 300:
        raise RuntimeError(f"extract too short ({len(text)} chars)")

    return {
        "title": title or url,
        "text": text,
        "char_count": len(text),
        "heading_count": len(headings),
    }


def render_markdown(item, run_date: str, pack_type: str, capture: dict) -> str:
    frontmatter = {
        "title": capture["title"],
        "source_title": item.title,
        "url": item.url,
        "domain": domain_of(item.url),
        "captured_at": datetime.now().astimezone().isoformat(),
        "briefing_run_date": run_date,
        "briefing_pack": pack_type,
        "source_type": item.source,
        "rating": item.rating,
        "score": item.score,
        "stream": item.stream,
        "item_id": item.item_id,
        "char_count": capture["char_count"],
    }
    yaml_lines = ["---"]
    for k, v in frontmatter.items():
        if v is None:
            yaml_lines.append(f"{k}: null")
        elif isinstance(v, (int, float)):
            yaml_lines.append(f"{k}: {v}")
        else:
            safe = str(v).replace('"', "'")
            yaml_lines.append(f'{k}: "{safe}"')
    yaml_lines.extend([
        "---",
        "",
        f"# {capture['title']}",
        "",
        f"Original URL: {item.url}",
        "",
        "## Briefing Context",
        f"- Rating: {item.rating}",
        f"- Score: {item.score}",
        f"- Stream: {item.stream}",
        f"- Source type: {item.source}",
        "",
        "## Extracted Content",
        "",
        capture["text"],
        "",
    ])
    return "\n".join(yaml_lines)


def iter_selected_items(run_date: str, mode: str, pipeline_cfg: dict):
    items = get_items_for_date(run_date)
    am_items, pm_items, _am_path, _pm_path = build_packs(items, run_date, pipeline_cfg, write_mode="ALL")
    if mode == "AM":
        return am_items
    if mode == "PM":
        return pm_items
    return am_items + pm_items


def capture_run(run_date: str, mode: str, output_root: Path, pipeline_cfg: dict, run_logger: Any = None) -> dict:
    selected = [item for item in iter_selected_items(run_date, mode, pipeline_cfg) if is_capture_candidate(item)]
    packs: list[str] = [mode] if mode in ("AM", "PM") else ["AM", "PM"]
    base_dir = output_root / run_date
    base_dir.mkdir(parents=True, exist_ok=True)

    results = []
    per_pack_counts = {"AM": 0, "PM": 0}

    for item in selected:
        pack = item.pack_assigned or ("AM" if item.source == "morning" else "PM")
        if mode != "ALL" and pack != mode:
            continue

        rec = {
            "item_id": item.item_id,
            "title": item.title,
            "url": item.url,
            "pack": pack,
            "source": item.source,
            "status": "skipped",
            "output_path": None,
            "error": None,
        }

        if not item.url:
            rec["status"] = "no_url"
            rec["error"] = "item has no url"
            results.append(rec)
            continue

        out_dir = base_dir / pack
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{item.item_id}_{slugify(item.title)}.md"
        out_path = out_dir / filename

        try:
            if run_logger:
                with run_logger.boundary(
                    "web",
                    "fetch_article_html",
                    url=item.url,
                    domain=domain_of(item.url),
                    item_id=item.item_id,
                    pack=pack,
                ):
                    html = fetch_html(item.url)
            else:
                html = fetch_html(item.url)
            if run_logger:
                run_logger.event(
                    "raw_capture_html_fetched",
                    status="ok",
                    item_id=item.item_id,
                    url=item.url,
                    domain=domain_of(item.url),
                    html_char_count=len(html),
                )
            capture = extract_article(html, item.url)
            out_path.write_text(render_markdown(item, run_date, pack, capture), encoding="utf-8")
            rec["status"] = "captured"
            rec["output_path"] = str(out_path)
            rec["char_count"] = capture["char_count"]
            per_pack_counts[pack] += 1
            if run_logger:
                run_logger.event(
                    "raw_capture_item_finish",
                    status="captured",
                    item_id=item.item_id,
                    pack=pack,
                    output_path=str(out_path),
                    char_count=capture["char_count"],
                    heading_count=capture.get("heading_count"),
                )
        except Exception as exc:
            rec["status"] = "failed"
            rec["error"] = str(exc)
            if run_logger:
                run_logger.event(
                    "raw_capture_item_error",
                    status="failed",
                    item_id=item.item_id,
                    pack=pack,
                    url=item.url,
                    domain=domain_of(item.url),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

        results.append(rec)

    manifest = {
        "run_date": run_date,
        "mode": mode,
        "captured_at": datetime.now().astimezone().isoformat(),
        "output_root": str(base_dir),
        "counts": {
            "total_items": len(selected),
            "captured": sum(1 for r in results if r["status"] == "captured"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "no_url": sum(1 for r in results if r["status"] == "no_url"),
            "by_pack": per_pack_counts,
        },
        "results": results,
        "kb_handoff": {
            "status": "ready" if any(r["status"] == "captured" for r in results) else "empty",
            "manifest_path": str(base_dir / "kb_handoff_manifest.json"),
            "raw_root": str(base_dir),
        },
    }

    manifest_path = base_dir / "kb_handoff_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if run_logger:
        run_logger.event(
            "raw_capture_manifest_written",
            status="ok",
            manifest_path=str(manifest_path),
            counts=manifest["counts"],
        )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture briefing article content into raw vault folders")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--mode", choices=["AM", "PM", "ALL"], default="ALL")
    parser.add_argument("--output-root", default=str(DEFAULT_VAULT_RAW))
    args = parser.parse_args()

    init_db()
    cfg_path = _HERE / "config.json"
    config = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {}
    pipeline_cfg = config.get("pipeline", {})

    manifest = capture_run(args.date, args.mode, Path(args.output_root), pipeline_cfg)
    print(json.dumps({
        "run_date": manifest["run_date"],
        "mode": manifest["mode"],
        "captured": manifest["counts"]["captured"],
        "failed": manifest["counts"]["failed"],
        "no_url": manifest["counts"]["no_url"],
        "output_root": manifest["output_root"],
        "manifest_path": manifest["kb_handoff"]["manifest_path"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

