#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/test_web_chat.py — pytest (unit, no browser)
"""web_chat CLI tests — prompt sourcing and account resolution.

Everything here runs without Playwright: `_read_prompt` is exercised through
fake stdins, and `main()` is stopped right after account resolution by a spy
so no browser is ever launched.
"""

from __future__ import annotations

import argparse
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import web_chat  # noqa: E402

QWEN = web_chat.AccountType.QWEN
DEEPSEEK = web_chat.AccountType.DEEPSEEK


class _Stdin:
    """Stand-in for sys.stdin so isatty() is controllable in tests."""

    def __init__(self, text: str = "", tty: bool = False):
        self._text = text
        self._tty = tty
        self.read_called = False

    def isatty(self) -> bool:
        return self._tty

    def readline(self) -> str:
        return self._text

    def read(self) -> str:
        # A TTY path must never reach the blocking read(); only the piped path may.
        self.read_called = True
        return self._text


def _args(**overrides) -> argparse.Namespace:
    base = {"prompt": None, "prompt_file": None}
    base.update(overrides)
    return argparse.Namespace(**base)


# ── _read_prompt ──────────────────────────────────────────────────────


def test_should_return_piped_stdin_when_not_a_tty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Stdin("  hello world  \n", tty=False))
    assert web_chat._read_prompt(_args(), QWEN) == "hello world"


def test_should_exit_when_piped_stdin_is_empty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Stdin("", tty=False))
    with pytest.raises(SystemExit, match="prompt is required"):
        web_chat._read_prompt(_args(), QWEN)


def test_should_prompt_on_a_tty_instead_of_blocking(monkeypatch, capsys):
    stdin = _Stdin("  typed prompt  \n", tty=True)
    monkeypatch.setattr(sys, "stdin", stdin)

    assert web_chat._read_prompt(_args(), QWEN) == "typed prompt"
    assert not stdin.read_called, "a TTY must fall back to input(), never read()"
    assert "qwen> " in capsys.readouterr().out


def test_should_exit_when_tty_input_is_blank(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Stdin("", tty=True))
    with pytest.raises(SystemExit, match="prompt is required"):
        web_chat._read_prompt(_args(), QWEN)


def test_should_prefer_positional_prompt_over_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Stdin("ignored\n", tty=True))
    assert web_chat._read_prompt(_args(prompt=["a", "b"]), QWEN) == "a b"


def test_should_prefer_prompt_file_over_stdin(monkeypatch, tmp_path):
    path = tmp_path / "prompt.txt"
    path.write_text("from file\n", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", _Stdin("ignored\n", tty=True))
    assert web_chat._read_prompt(_args(prompt_file=str(path)), QWEN) == "from file"


# ── main(): account resolution (browser never reached) ────────────────


def _argv(monkeypatch, *flags: str) -> None:
    monkeypatch.setattr(sys, "argv", ["web_chat.py", *flags])


def _spy_account(monkeypatch) -> list:
    """Record the account main() resolved, then abort before CliConfig/run."""
    seen: list = []

    def _spy(_args, account_type):
        seen.append(account_type)
        raise SystemExit(0)

    monkeypatch.setattr(web_chat, "_read_prompt", _spy)
    return seen


def test_should_reject_unknown_account_when_url_alias_is_bad(monkeypatch, capsys):
    _argv(monkeypatch, "--url", "bogus")
    assert web_chat.main() == 1
    assert "Invalid account type" in capsys.readouterr().err


def test_should_prefer_account_over_the_url_alias(monkeypatch):
    seen = _spy_account(monkeypatch)
    _argv(monkeypatch, "--account", "deepseek", "--url", "qwen")
    with pytest.raises(SystemExit):
        web_chat.main()
    assert seen == [DEEPSEEK]


def test_should_fall_back_to_the_url_alias_when_account_absent(monkeypatch):
    seen = _spy_account(monkeypatch)
    _argv(monkeypatch, "--url", "deepseek")
    with pytest.raises(SystemExit):
        web_chat.main()
    assert seen == [DEEPSEEK]


def test_should_default_to_qwen_when_no_account_given(monkeypatch):
    seen = _spy_account(monkeypatch)
    _argv(monkeypatch)
    with pytest.raises(SystemExit):
        web_chat.main()
    assert seen == [QWEN]
    assert seen[0] is web_chat.DEFAULT_ACCOUNT
