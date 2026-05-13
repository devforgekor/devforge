"""
core/text | Text normalization and token counting: clean whitespace, count tokens via tiktoken or word fallback | needs:tiktoken(optional) | clean_text(),count_tokens()
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
