"""
core/text | Text normalization and token counting: clean whitespace, normalize for IDs, strip HTML, count tokens via tiktoken or word fallback | needs:tiktoken(optional) | clean_text(),normalize(),normalize_for_id(),strip_html(),count_tokens()
"""
import re
import unicodedata
from typing import Any


def clean_text(text: str) -> str:
    """Normalize unicode (NFC) and collapse whitespace.

    Returns an empty string for non-string input.
    """
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_html(text: str) -> str:
    """Remove HTML tags from text."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"<[^>]*>", " ", text).strip()


def normalize(text: str, strip_html_tags: bool = False) -> str:
    """NFKC unicode normalization with optional HTML stripping.

    Also removes non-breaking spaces (U+00A0), zero-width spaces (U+200B),
    BOM (U+FEFF), and collapses all whitespace to single spaces.
    """
    if not isinstance(text, str):
        return ""
    s = text
    if strip_html_tags:
        s = re.sub(r"<[^>]*>", " ", s)
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u00A0", " ").replace("\u200b", "").replace("\ufeff", "")
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_for_id(text: str) -> str:
    """Normalize text for use as an ID: strip HTML, remove whitespace and symbols, lowercase."""
    if not isinstance(text, str):
        return ""
    t = normalize(text, strip_html_tags=True)
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[^a-zA-Z0-9\uAC00-\uD7A3]", "", t)
    return t.lower()


def count_tokens(messages: list[dict[str, Any]], model: str = "gpt-3.5-turbo") -> int:
    """Count tokens across a list of chat messages.

    Uses tiktoken when available; falls back to whitespace word count.
    Each message should be a dict with 'role' and 'content' keys.
    """
    try:
        import tiktoken
        try:
            enc = tiktoken.encoding_for_model(model)
        except Exception:
            enc = tiktoken.get_encoding("cl100k_base")
        total = 0
        for msg in messages:
            text = f"{msg.get('role', '')}: {msg.get('content', '')}"
            total += len(enc.encode(text))
        return total
    except ImportError:
        total = 0
        for msg in messages:
            text = f"{msg.get('role', '')}: {msg.get('content', '')}"
            total += len(text.split())
        return total
