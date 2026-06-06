"""
Push unpublished Claude Code sessions to Azure Service Bus.

Reads ~/.claude/projects/<project>/*.jsonl, sends messages to Service Bus
queue "seedling-inbox", and tracks sent sessions in .pushed_sessions.

Usage:
    python scripts/push_claude_sessions.py          # all unsent sessions
    python scripts/push_claude_sessions.py --dry-run # preview only
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

# Load .env from seedling project root (needed when run from Claude Code hook)
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_SB_CONN_STR = os.getenv("SERVICE_BUS_CONNECTION_STRING")
_QUEUE_NAME = os.getenv("SERVICE_BUS_QUEUE_NAME", "seedling-inbox")
_USER_ID = os.getenv("CLAUDE_DEV_USER_ID", "dev-claude-cli-mac")


def _get_project_dir() -> Path:
    cwd = os.getcwd()
    slug = cwd.replace("/", "-")  # /Users/me/proj → -Users-me-proj
    return Path.home() / ".claude" / "projects" / slug


def _read_pushed_sessions(track_file: Path) -> set:
    if not track_file.exists():
        return set()
    return set(track_file.read_text().strip().splitlines())


def _write_pushed_sessions(track_file: Path, sessions: set):
    track_file.write_text("\n".join(sorted(sessions)) + "\n")


def _build_messages(jsonl_path: Path) -> list[dict]:
    """Convert a JSONL session into Service Bus messages (one per meaningful line)."""
    messages = []
    session_id = jsonl_path.stem
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type", "")
            if entry_type in ("permission-mode", "file-history-snapshot", "ai-title"):
                continue

            msg = entry.get("message", {})
            msg_uuid = entry.get("uuid") or msg.get("id", "")
            ts = entry.get("timestamp", "")

            if entry_type == "user":
                content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                if not content:
                    continue
                messages.append({
                    "userId": _USER_ID,
                    "turn_id": f"cc-{msg_uuid[:8]}",
                    "role": "user",
                    "content": content,
                    "source": "dev-claude-cli-mac",
                    "session_id": session_id,
                    "created_at": ts,
                    "model": entry.get("model", ""),
                    "cwd": entry.get("cwd", ""),
                    "version": entry.get("version", ""),
                    "gitBranch": entry.get("gitBranch", ""),
                })

            elif entry_type == "assistant":
                if not isinstance(msg, dict):
                    continue
                content_blocks = msg.get("content", [])
                if isinstance(content_blocks, str):
                    content_blocks = [{"type": "text", "text": content_blocks}]
                if not isinstance(content_blocks, list):
                    continue

                thinking_parts = []
                text_parts = []
                for block in content_blocks:
                    if block.get("type") == "thinking":
                        thinking_parts.append(block.get("thinking", ""))
                    elif block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_name = block.get("name", "unknown")
                        tool_input = json.dumps(block.get("input", {}), ensure_ascii=False)
                        text_parts.append(f"[tool_use: {tool_name}] {tool_input}")

                thought_text = "\n".join(thinking_parts).strip()
                answer_text = "\n".join(text_parts).strip()

                if answer_text:
                    messages.append({
                        "userId": _USER_ID,
                        "turn_id": f"cc-{msg_uuid[:8]}",
                        "role": "assistant" if not thought_text else "assistant",
                        "content": answer_text,
                        "thought_text": thought_text,
                        "source": "dev-claude-cli-mac",
                        "session_id": session_id,
                        "created_at": ts,
                        "model": entry.get("model", ""),
                        "cwd": entry.get("cwd", ""),
                        "version": entry.get("version", ""),
                        "gitBranch": entry.get("gitBranch", ""),
                    })

            elif entry_type == "system":
                content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                if not content:
                    continue
                messages.append({
                    "userId": _USER_ID,
                    "turn_id": f"cc-{msg_uuid[:8]}",
                    "role": "system",
                    "content": content,
                    "source": "dev-claude-cli-mac",
                    "session_id": session_id,
                    "created_at": ts,
                })

    return messages


def _send_to_service_bus(messages: list[dict]) -> int:
    """Send messages to Azure Service Bus. Returns count of sent messages."""
    if not _SB_CONN_STR:
        print("[push_claude] SERVICE_BUS_CONNECTION_STRING not set. Set it in .env or export.")
        return 0

    from azure.servicebus import ServiceBusClient, ServiceBusMessage

    sent = 0
    try:
        client = ServiceBusClient.from_connection_string(_SB_CONN_STR)
        sender = client.get_queue_sender(queue_name=_QUEUE_NAME)

        # Batch send (max 100 per batch)
        batch_size = 100
        for i in range(0, len(messages), batch_size):
            chunk = messages[i:i + batch_size]
            sb_messages = [
                ServiceBusMessage(
                    json.dumps(m, ensure_ascii=False, default=str)
                )
                for m in chunk
            ]
            sender.send_messages(sb_messages)
            sent += len(chunk)
            print(f"  [push_claude] sent {sent}/{len(messages)}")

        client.close()
    except Exception as e:
        print(f"[push_claude] Service Bus send error: {e}")
    return sent


def main():
    parser = argparse.ArgumentParser(description="Push Claude Code sessions to Service Bus")
    parser.add_argument("--dry-run", action="store_true", help="Preview without sending")
    args = parser.parse_args()

    project_dir = _get_project_dir()
    if not project_dir.exists():
        print(f"[push_claude] No Claude Code sessions directory: {project_dir}")
        return

    track_file = project_dir / ".pushed_sessions"
    pushed = _read_pushed_sessions(track_file)

    jsonl_files = sorted(project_dir.glob("*.jsonl"))
    new_files = [f for f in jsonl_files if f.stem not in pushed]

    # Skip files modified in the last 60s (currently being written to)
    now = time.time()
    new_files = [f for f in new_files if (now - f.stat().st_mtime) > 60]

    if not new_files:
        print("[push_claude] No new sessions to push.")
        return

    print(f"[push_claude] Found {len(new_files)} new session(s):")
    total_messages = 0
    all_msgs = []

    for jf in new_files:
        messages = _build_messages(jf)
        total_messages += len(messages)
        all_msgs.extend(messages)
        print(f"  {jf.stem}: {len(messages)} messages")

    if args.dry_run:
        # Show sample messages
        for m in all_msgs[:5]:
            print(f"\n  [{m['role']}] {m['content'][:120]}...")
            if m.get("thought_text"):
                print(f"    thought: {m['thought_text'][:100]}...")
        print(f"\n[push_claude] DRY RUN — {total_messages} messages not sent.")
        return

    sent_count = _send_to_service_bus(all_msgs)
    if sent_count > 0:
        for jf in new_files:
            pushed.add(jf.stem)
        _write_pushed_sessions(track_file, pushed)
        print(f"[push_claude] Done: {sent_count}/{total_messages} messages from {len(new_files)} sessions.")
    else:
        print("[push_claude] Nothing sent. Check connection or dry-run first.")


if __name__ == "__main__":
    main()
