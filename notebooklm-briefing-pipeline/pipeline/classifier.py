"""
Stream classifier.

Maps a BriefingItem to one of the six learning streams using a
keyword/score heuristic. No external dependencies.

Streams (in priority order for pack building):
  Ashish's Priority Reads  – highest-value items (Essential rating OR score >= threshold)
  AI Agents                – agentic / autonomous systems
  AI Research              – papers, benchmarks, model training
  AI Policy                – regulation, governance, law
  AI Products              – launches, APIs, consumer tools
  AI Case Studies          – enterprise deployments, ROI stories
"""
from __future__ import annotations

from typing import List

from .models import BriefingItem, STREAMS

# Score above which an item is always Priority (interpreted on a 0-10 scale)
_PRIORITY_SCORE = 9.0

# Keyword lists per stream (lowercase). Evaluated in order; first match wins
# when there's a tie-break. More specific terms should come first.
_KEYWORDS: dict[str, List[str]] = {
    "AI Agents": [
        "agentic", "multi-agent", "multi agent", "autonomous agent",
        "agent framework", "tool use", "tool-use", "computer use",
        "browser use", "langgraph", "autogen", "crewai", "manus",
        "devin", "swe-agent", "agentbench", "opendevin",
        "orchestrat", "workflow agent", "agent sdk",
    ],
    "AI Research": [
        "arxiv", "paper", "preprint", "benchmark", "pre-train",
        "fine-tun", "fine tune", "dataset", "evaluation", "eval ",
        "scaling law", "reasoning model", "emergent", "capability",
        "model weight", "open weight", "alignment research",
        "rlhf", "rlaif", "dpo ", "sft ", "inference time",
    ],
    "AI Policy": [
        "regulation", "regulator", "congress", "senate", "house bill",
        "eu ai act", "ai act", "nist", "white house", "executive order",
        "executive action", "ftc ", "doj ", "antitrust",
        "governance", "compliance", "policy", "legislation",
        "safety board", "ban ", "restrict", "liability", "accountability",
    ],
    "AI Products": [
        "launch", "release", "product", "api launch", "api update",
        "generally available", " ga ", "public beta", "preview",
        "chatgpt", "gemini", "copilot", "claude", "grok", "llama",
        "openai", "anthropic", "google deepmind", "microsoft",
        "amazon bedrock", "sagemaker", "vertex ai",
    ],
    "AI Case Studies": [
        "enterprise", "deployment", "deployed", "production",
        "use case", "case study", "roi ", "return on investment",
        "workflow automation", "cost saving", "headcount",
        "adoption", "implementation", "customer story",
        "pilot program", "rollout",
    ],
}


def classify_item(item: BriefingItem, priority_score: float = _PRIORITY_SCORE) -> str:
    """
    Assign the best-fit stream label to a BriefingItem.

    Priority Read rule fires first (before keyword matching):
      rating == Essential  OR  score >= priority_score
    """
    normalized_score = item.score_out_of_10
    if item.rating == "Essential" or (normalized_score is not None and normalized_score >= priority_score):
        return "Ashish's Priority Reads"

    # Build a single lowercase search corpus from all text fields
    corpus = " ".join(
        filter(
            None,
            [
                item.title,
                item.summary,
                " ".join(item.why_bullets or []),
                item.leader_move or "",
            ],
        )
    ).lower()

    # Count keyword hits per stream
    scores: dict[str, int] = {s: 0 for s in _KEYWORDS}
    for stream, keywords in _KEYWORDS.items():
        for kw in keywords:
            if kw in corpus:
                scores[stream] += 1

    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "AI Products"  # fallback


def classify_all(
    items: List[BriefingItem],
    priority_score: float = _PRIORITY_SCORE,
) -> List[BriefingItem]:
    """Classify every item in-place; returns the same list."""
    for item in items:
        if not item.stream:
            item.stream = classify_item(item, priority_score)
    return items

