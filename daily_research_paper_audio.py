from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

WORKSPACE = Path(r"C:\path\to\workspace")
NOTEBOOKLM_ROOT = WORKSPACE / "notebooklm-briefing-pipeline"
OUTPUT_DIR = WORKSPACE / "research-paper-audio-outputs"
CONFIG_PATH = NOTEBOOKLM_ROOT / "config.json"
HISTORY_PATH = WORKSPACE / "research-paper-audio-history.json"

sys.path.insert(0, str(NOTEBOOKLM_ROOT))
from pipeline.db import init_db, log_publish_run  # noqa: E402
from pipeline.notebooklm_adapter import NotebookLMAdapter  # noqa: E402

TRENDING_URL = "https://huggingface.co/papers/trending"
HF_PAPER_URL = "https://huggingface.co/papers/{paper_id}"
ARXIV_ABS_URL = "https://arxiv.org/abs/{paper_id}"
ARXIV_HTML_URL = "https://arxiv.org/html/{paper_id}"

AUDIO_FOCUS_PROMPT = (
    "Generate a high-signal technical audio overview of this single AI research paper. "
    "Target about 18-22 minutes. Do not waste time restating instructions or reading metadata. "
    "Start with the problem, why it matters, and the core contribution. Then walk through the method, "
    "key architecture choices, training/setup assumptions, benchmarks, strengths, failure modes, and limitations. "
    "Be concrete about what is genuinely novel versus incremental. Call out evidence quality, tradeoffs, and open questions. "
    "End with what an advanced AI operator, researcher, or product leader should take away from this paper."
)

USER_AGENT = "Mozilla/5.0 (compatible; T800ResearchBot/1.0)"


def build_feed_url(config: dict[str, Any]) -> str:
    bridge_cfg = config.get("podcast_bridge", {})
    base_url = str(bridge_cfg.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("podcast_bridge.base_url is not configured")
    return f"{base_url}/feed.xml"


def parse_notebook_id(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/notebook/([^/?#]+)", url)
    return unquote(match.group(1)) if match else None


def verify_research_feed(run_date: str, notebook_url: str, config: dict[str, Any]) -> tuple[bool, str, str]:
    feed_url = build_feed_url(config)
    notebook_id = parse_notebook_id(notebook_url)
    if not notebook_id:
        return False, feed_url, "could not parse notebook id"

    try:
        feed_xml = fetch(feed_url)
    except Exception as exc:
        return False, feed_url, f"feed fetch failed ({str(exc).strip()})"

    expected_guid = f"{run_date}:RESEARCH:{notebook_id}"
    if expected_guid not in feed_xml:
        return False, feed_url, f"research episode not yet present in feed ({expected_guid})"

    return True, feed_url, "ok"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="ignore")


def clean_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[(?:\d+|edit|citation needed)\]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_block_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def load_history() -> dict[str, Any]:
    if not HISTORY_PATH.exists():
        return {"reviewed_paper_ids": [], "review_log": []}

    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"reviewed_paper_ids": [], "review_log": []}

    data.setdefault("reviewed_paper_ids", [])
    data.setdefault("review_log", [])
    return data


def save_history(history: dict[str, Any]) -> None:
    HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")


def extract_trending_candidates() -> list[dict[str, Any]]:
    page = fetch(TRENDING_URL)
    matches = re.finditer(r'/papers/(\d{4}\.\d{5}).{0,2000}?<h3[^>]*>(.*?)</h3>', page, flags=re.I | re.S)

    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for match in matches:
        paper_id = match.group(1)
        if paper_id in seen_ids:
            continue
        seen_ids.add(paper_id)

        title = clean_block_text(re.sub(r"<[^>]+>", " ", match.group(2))) or paper_id
        summary_match = re.search(
            rf'/papers/{re.escape(paper_id)}.*?<p[^>]*class="[^"]*text-gray-500[^"]*"[^>]*>(.*?)</p>',
            page,
            flags=re.I | re.S,
        )
        summary = clean_block_text(re.sub(r"<[^>]+>", " ", summary_match.group(1))) if summary_match else ""

        candidates.append(
            {
                "paper_id": paper_id,
                "title": title,
                "trending_summary": summary,
                "trending_url": TRENDING_URL,
                "hf_url": HF_PAPER_URL.format(paper_id=paper_id),
                "arxiv_abs_url": ARXIV_ABS_URL.format(paper_id=paper_id),
                "arxiv_html_url": ARXIV_HTML_URL.format(paper_id=paper_id),
            }
        )

    if not candidates:
        raise RuntimeError("could not find any trending paper candidates on Hugging Face")

    return candidates


def pick_fresh_trending_paper(reviewed_paper_ids: set[str]) -> dict[str, Any]:
    candidates = extract_trending_candidates()
    fresh_candidates = [candidate for candidate in candidates if candidate["paper_id"] not in reviewed_paper_ids]
    if not fresh_candidates:
        raise RuntimeError("no fresh trending paper available; all current Hugging Face trending candidates were already reviewed")
    return fresh_candidates[0]


def extract_abs_metadata(abs_html: str) -> dict[str, Any]:
    title_match = re.search(r"<title>\s*\[(.*?)\]\s*(.*?)</title>", abs_html, flags=re.I | re.S)
    abstract_match = re.search(r"Abstract:</span>(.*?)</blockquote>", abs_html, flags=re.I | re.S)
    authors_match = re.search(r"Authors:</span>(.*?)</div>", abs_html, flags=re.I | re.S)
    subjects_match = re.search(r"Subjects:</span>(.*?)</td>", abs_html, flags=re.I | re.S)

    return {
        "title_from_abs": clean_text(title_match.group(2)) if title_match else "",
        "abstract": clean_text(abstract_match.group(1)) if abstract_match else "",
        "authors": clean_text(authors_match.group(1)) if authors_match else "",
        "subjects": clean_text(subjects_match.group(1)) if subjects_match else "",
    }


def extract_html_excerpt(full_html: str, max_chars: int = 22000) -> str:
    text = clean_text(full_html)
    markers = [
        "Abstract",
        "1 Introduction",
        "2 Method",
        "3 Experiments",
        "4 Results",
        "5 Conclusion",
        "References",
    ]

    start = 0
    for marker in markers:
        idx = text.find(marker)
        if idx != -1:
            start = idx
            break

    excerpt = text[start:start + max_chars]
    return excerpt.strip()


def build_markdown(run_dt: datetime, paper: dict[str, Any], meta: dict[str, Any], excerpt: str) -> str:
    title = meta.get("title_from_abs") or paper["title"]
    abstract = meta.get("abstract") or paper.get("trending_summary") or ""
    lines = [
        "---",
        f'title: "Trending AI Research Paper - {title}"',
        f'date: "{run_dt.date().isoformat()}"',
        f'paper_id: "{paper["paper_id"]}"',
        f'generated_at: "{run_dt.isoformat()}"',
        'source: "Hugging Face Trending Papers + arXiv"',
        "---",
        "",
        f"# Trending AI Research Paper of the Day",
        "",
        f"## {title}",
        "",
        f"- **Paper ID:** {paper['paper_id']}",
        f"- **Why selected:** first visible paper on Hugging Face Trending Papers today",
        f"- **Hugging Face:** {paper['hf_url']}",
        f"- **arXiv:** {paper['arxiv_abs_url']}",
        f"- **Subjects:** {meta.get('subjects', 'Unknown')}",
        f"- **Authors:** {meta.get('authors', 'Unknown')}",
        "",
        "## Short trend snapshot",
        paper.get("trending_summary") or abstract,
        "",
        "## Abstract",
        abstract,
        "",
        "## Audio goals",
        "This overview should explain the paper clearly, focus on the real technical contribution, summarize the method and evidence, and highlight limitations and practical implications.",
        "",
        "## Questions the audio should answer",
        "- What problem does this paper solve, and why does it matter now?",
        "- What is the core technical idea?",
        "- What evidence does the paper provide?",
        "- Where might the claims be strong, weak, or uncertain?",
        "- What should researchers, builders, or product leaders take away?",
        "",
        "## Paper excerpt for NotebookLM context",
        excerpt,
        "",
    ]
    return "\n".join(lines)


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


def run(publish: bool = True) -> tuple[Path, dict[str, Any], str | None, str | None, str]:
    run_dt = datetime.now()
    run_date = run_dt.date().isoformat()
    OUTPUT_DIR.mkdir(exist_ok=True)

    history = load_history()
    reviewed_paper_ids = set(history.get("reviewed_paper_ids", []))
    candidates = extract_trending_candidates()

    last_error: Exception | None = None
    paper: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None
    excerpt: str | None = None

    for candidate in candidates:
        if candidate["paper_id"] in reviewed_paper_ids:
            continue
        try:
            abs_html = fetch(candidate["arxiv_abs_url"])
            full_html = fetch(candidate["arxiv_html_url"])
            meta = extract_abs_metadata(abs_html)
            excerpt = extract_html_excerpt(full_html)
            paper = candidate
            break
        except urllib.error.HTTPError as exc:
            last_error = exc
            continue

    if paper is None or meta is None or excerpt is None:
        if last_error is not None:
            raise RuntimeError(f"no fresh paper with valid source pages was available ({last_error})")
        raise RuntimeError("no fresh trending paper available; all current candidates were already reviewed")

    md = build_markdown(run_dt, paper, meta, excerpt)
    output_path = OUTPUT_DIR / f"{run_date}_{paper['paper_id'].replace('.', '_')}_research_paper.md"
    output_path.write_text(md, encoding="utf-8")

    notebook_url = None
    audio_status = None
    if publish:
        config = load_config()
        notebook_cfg = dict(config.get("notebooklm", {}))
        notebook_cfg.update(
            {
                "default_notebook_name": f"Research Paper of the Day - {run_date}",
                "audio_focus_prompt": AUDIO_FOCUS_PROMPT,
                "audio_length": "default",
                "audio_format": "deep_dive",
                "public_by_default": True,
            }
        )
        adapter = NotebookLMAdapter(notebook_cfg)
        notebook_url, audio_status = adapter.publish_pack(
            output_path,
            notebook_name=f"Research Paper of the Day - {paper['title']}",
        )

        init_db()
        feed_ready, feed_url, feed_detail = verify_research_feed(run_date, notebook_url, config)
        base_message = (
            f"NOTEBOOKLM_RESEARCH_READY_NO_AUDIO: {notebook_url} | paper={paper['title']} | quota reached"
            if audio_status == "quota_reached"
            else f"NOTEBOOKLM_RESEARCH_READY: {notebook_url} | paper={paper['title']} | audio_status={audio_status}"
        )
        publish_message = (
            f"{base_message} | APPLE_FEED_READY: {feed_url}"
            if feed_ready
            else f"{base_message} | APPLE_FEED_PENDING: {feed_detail}"
        )
        publish_status = "published_no_audio" if audio_status == "quota_reached" else "published_audio_requested"
        log_publish_run(
            run_date=run_date,
            pack_type="RESEARCH",
            status=publish_status,
            notebook_url=notebook_url,
            message=publish_message,
            output_path=str(output_path),
        )

    if publish:
        if paper["paper_id"] not in reviewed_paper_ids:
            history.setdefault("reviewed_paper_ids", []).append(paper["paper_id"])
        history.setdefault("review_log", []).append(
            {
                "date": run_date,
                "paper_id": paper["paper_id"],
                "title": paper["title"],
                "output_file": str(output_path),
                "notebook_url": notebook_url,
            }
        )
        save_history(history)

    return output_path, paper, notebook_url, audio_status, run_date


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate daily trending AI research paper overview and publish to NotebookLM")
    parser.add_argument("--no-publish", action="store_true", help="Only build the markdown pack")
    args = parser.parse_args()

    try:
        output_path, paper, notebook_url, audio_status, run_date = run(publish=not args.no_publish)
    except Exception as exc:
        print(f"RESEARCH_PAPER_ERROR: {str(exc).strip()}")
        return 1

    print(f"RESEARCH_PAPER_FILE: {output_path}")
    print(f"RESEARCH_PAPER_PICK: {paper['title']} ({paper['paper_id']})")
    if notebook_url:
        config = load_config()
        feed_ready, feed_url, feed_detail = verify_research_feed(run_date, notebook_url, config)
        if audio_status == "quota_reached":
            base_message = f"NOTEBOOKLM_RESEARCH_READY_NO_AUDIO: {notebook_url} | paper={paper['title']} | quota reached"
        else:
            base_message = f"NOTEBOOKLM_RESEARCH_READY: {notebook_url} | paper={paper['title']} | audio_status={audio_status}"
        if feed_ready:
            print(f"{base_message} | APPLE_FEED_READY: {feed_url}")
        else:
            print(f"{base_message} | APPLE_FEED_PENDING: {feed_detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

