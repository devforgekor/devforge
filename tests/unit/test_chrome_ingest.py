#!/usr/bin/env python3
# Status: production
# Path: tests/unit/test_chrome_ingest.py — pytest
"""Unit tests for the chrome-web-llm → /api/v1/ingest push bridge.

Why a dedicated file: this bridge is the only component that turns raw capture
rows into the ingest contract, so idempotency (stable conversation_id /
source_message_id) and provenance (source=chrome[:model]) must be pinned here —
they are invisible until a duplicate or mislabeled row shows up in `turns`.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import chrome_ingest  # noqa: E402


def make_row(role: str, text: str, model: str = "qwen", ts: str = "2026-09-20T16:35:45Z") -> dict:
    return {"ts": ts, "model": model, "role": role, "text": text}


def write_session(tmp_path: Path, name: str, rows: list[dict]) -> Path:
    path = tmp_path / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


# ── pairing ──────────────────────────────────────────────────────────────────


def test_should_pair_user_and_assistant_when_session_has_two_turns() -> None:
    rows = [
        make_row("user", "What is 2+2?"),
        make_row("assistant", "4"),
        make_row("user", "And times 3?"),
        make_row("assistant", "12"),
    ]
    turns = chrome_ingest.pair_turns(rows, "chat")

    assert len(turns) == 2
    assert turns[0]["user_turn"] == "What is 2+2?"
    assert turns[0]["text"] == "4"
    assert turns[1]["user_turn"] == "And times 3?"
    assert turns[1]["text"] == "12"
    assert [t["seq"] for t in turns] == [1, 2]


def test_should_keep_half_turn_when_assistant_row_is_missing() -> None:
    rows = [make_row("user", "orphan question")]
    turns = chrome_ingest.pair_turns(rows, "chat")

    assert len(turns) == 1
    assert turns[0]["user_turn"] == "orphan question"
    assert turns[0]["text"] is None


def test_should_keep_half_turn_when_user_row_is_missing() -> None:
    rows = [make_row("assistant", "unsolicited answer")]
    turns = chrome_ingest.pair_turns(rows, "chat")

    assert len(turns) == 1
    assert turns[0]["user_turn"] == ""
    assert turns[0]["text"] == "unsolicited answer"


# ── provenance ───────────────────────────────────────────────────────────────


def test_should_use_model_qualified_source_when_session_uses_one_model() -> None:
    rows = [make_row("user", "q"), make_row("assistant", "a", model="qwen")]
    payload = chrome_ingest.build_payload("chat", rows)

    assert payload["source"] == "chrome:qwen"
    assert payload["model"] == "qwen"
    assert payload["agent"] == "chrome-web-llm"


def test_should_use_bare_chrome_source_when_session_mixes_models() -> None:
    rows = [
        make_row("user", "q", model="qwen"),
        make_row("assistant", "a", model="qwen"),
        make_row("user", "q2", model="deepseek"),
        make_row("assistant", "a2", model="deepseek"),
    ]
    payload = chrome_ingest.build_payload("chat", rows)

    assert payload["source"] == "chrome"
    assert payload["model"] == ""
    assert [t["meta"]["model"] for t in payload["turns"]] == ["qwen", "deepseek"]


# ── idempotency ──────────────────────────────────────────────────────────────


def test_should_emit_identical_ids_when_payload_built_twice() -> None:
    rows = [make_row("user", "q"), make_row("assistant", "a")]

    first = chrome_ingest.build_payload("chat", rows)
    second = chrome_ingest.build_payload("chat", rows)

    assert first["conversation_id"] == second["conversation_id"]
    assert [t["source_message_id"] for t in first["turns"]] == [
        t["source_message_id"] for t in second["turns"]
    ]


def test_should_emit_distinct_conversation_id_when_session_name_differs() -> None:
    rows = [make_row("user", "q"), make_row("assistant", "a")]

    one = chrome_ingest.build_payload("chat", rows)
    two = chrome_ingest.build_payload("other", rows)

    assert one["conversation_id"] != two["conversation_id"]


# ── governance gate / parsing ────────────────────────────────────────────────


def test_should_drop_empty_text_rows_when_governance_filter_applies(tmp_path: Path) -> None:
    path = write_session(
        tmp_path,
        "chat",
        [
            make_row("user", "   "),
            make_row("assistant", ""),
            make_row("user", "real"),
            make_row("assistant", "answer"),
        ],
    )
    rows = chrome_ingest.load_rows(path)

    assert len(rows) == 2
    assert all(r["text"] for r in rows)


def test_should_skip_malformed_json_line_when_parsing(tmp_path: Path) -> None:
    path = tmp_path / "chat.jsonl"
    path.write_text(
        '{"ts":"t","model":"qwen","role":"user","text":"q"}\nnot json at all\n',
        encoding="utf-8",
    )
    rows = chrome_ingest.load_rows(path)

    assert len(rows) == 1


def test_should_drop_unknown_role_rows_when_parsing(tmp_path: Path) -> None:
    path = write_session(
        tmp_path,
        "chat",
        [
            {"ts": "t", "model": "qwen", "role": "system", "text": "system prompt"},
            make_row("user", "q"),
            make_row("assistant", "a"),
        ],
    )
    assert len(chrome_ingest.load_rows(path)) == 2


# ── contract ─────────────────────────────────────────────────────────────────


def test_should_satisfy_ingest_contract_when_payload_built() -> None:
    rows = [make_row("user", "q"), make_row("assistant", "a")]
    payload = chrome_ingest.build_payload("chat", rows)

    for key in ("source", "agent", "title", "model", "conversation_id", "turns"):
        assert key in payload
    assert isinstance(payload["conversation_id"], str)
    assert len(payload["conversation_id"]) == 36
    turn = payload["turns"][0]
    assert isinstance(turn["seq"], int)
    assert isinstance(turn["user_turn"], str)
    assert isinstance(turn["text"], str)
    assert isinstance(turn["meta"], dict)
    assert turn["source_message_id"]


# ── transport (fail-silent capture boundary) ─────────────────────────────────


class _CaptureHandler(BaseHTTPRequestHandler):
    bodies: list[bytes] = []

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length", "0"))
        type(self).bodies.append(self.rfile.read(length))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"inserted": 2, "skipped": 0}')

    def log_message(self, *args: object) -> None:
        pass


def _serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}/api/v1/ingest"


def test_should_post_payload_when_endpoint_reachable(tmp_path: Path) -> None:
    write_session(tmp_path, "chat", [make_row("user", "q"), make_row("assistant", "a")])
    _CaptureHandler.bodies = []
    server, endpoint = _serve()
    try:
        code = chrome_ingest.main(
            ["chat", "--data-dir", str(tmp_path), "--endpoint", endpoint]
        )
    finally:
        server.shutdown()

    assert code == 0
    assert len(_CaptureHandler.bodies) == 1
    posted = json.loads(_CaptureHandler.bodies[0])
    assert posted["source"] == "chrome:qwen"
    assert posted["conversation_id"]


def test_should_fail_silent_when_endpoint_unreachable(tmp_path: Path) -> None:
    write_session(tmp_path, "chat", [make_row("user", "q")])
    code = chrome_ingest.main(
        ["chat", "--data-dir", str(tmp_path), "--endpoint", "http://127.0.0.1:9/api/v1/ingest"]
    )

    assert code == 0


def test_should_return_one_when_strict_and_endpoint_unreachable(tmp_path: Path) -> None:
    write_session(tmp_path, "chat", [make_row("user", "q")])
    code = chrome_ingest.main(
        [
            "chat",
            "--data-dir",
            str(tmp_path),
            "--endpoint",
            "http://127.0.0.1:9/api/v1/ingest",
            "--strict",
        ]
    )

    assert code == 1


def test_should_return_one_when_session_file_missing(tmp_path: Path) -> None:
    code = chrome_ingest.main(["nope", "--data-dir", str(tmp_path)])

    assert code == 1


def test_should_print_payload_when_dry_run(tmp_path: Path, capsys) -> None:
    write_session(tmp_path, "chat", [make_row("user", "q"), make_row("assistant", "a")])
    code = chrome_ingest.main(["chat", "--data-dir", str(tmp_path), "--dry-run"])

    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["conversation_id"]


def test_should_error_when_neither_session_nor_all_given(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(SystemExit):
        chrome_ingest.main(["--data-dir", str(tmp_path)])
