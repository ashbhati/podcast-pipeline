"""
Deduplication utilities.

Primary dedup is enforced at the DB layer (item_id PRIMARY KEY).
This module provides URL normalization helpers used before insert.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode


# Query parameters to strip (tracking/UTM params)
_STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "referrer", "source", "via", "from", "mc_cid", "mc_eid",
    "fbclid", "gclid", "igshid", "_hsenc", "_hsmi", "hsCtaTracking",
}


def normalize_url(url: str) -> str:
    """
    Normalize a URL for deduplication purposes:
    - Lowercase scheme and host
    - Remove trailing slashes from path
    - Strip common tracking query params
    - Remove URL fragment
    """
    if not url:
        return url
    try:
        parsed = urlparse(url.strip())
        # Clean query params
        qs = parse_qs(parsed.query, keep_blank_values=False)
        cleaned_qs = {k: v for k, v in qs.items() if k.lower() not in _STRIP_PARAMS}
        clean_query = urlencode(cleaned_qs, doseq=True)

        normalized = urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                parsed.params,
                clean_query,
                "",  # remove fragment
            )
        )
        return normalized
    except Exception:
        return url


def is_noise_url(url: str) -> bool:
    """
    Return True for URLs that should be treated as weak (no URL) content.
    Catches paywalled or low-value link domains.
    """
    if not url:
        return True
    noise_patterns = [
        r"twitter\.com/",
        r"x\.com/",
        r"linkedin\.com/",
        r"facebook\.com/",
        r"instagram\.com/",
    ]
    for pat in noise_patterns:
        if re.search(pat, url, re.IGNORECASE):
            return True
    return False

