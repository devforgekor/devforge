#!/usr/bin/env python3
# Status: experimental
# Path: none — library
"""web_chat.py — DeepSeek web chat CLI via Playwright.

This is an isolated experimental tool for using the DeepSeek web UI from a
server. It is not wired into the existing session ingestion pipeline.
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple

DEFAULT_URL = "https://chat.deepseek.com/"
DEFAULT_TIMEOUT_MS = 120000
DEFAULT_WAIT_S = 120
DEFAULT_STORAGE_STATE = Path.home() / ".config/devforge/deepseek-storage-state.json"
DEFAULT_USER_DATA_DIR = Path.home() / ".cache/devforge/deepseek-profile"

INPUT_SELECTORS = [
    "textarea",
    "[contenteditable='true']",
]

SEND_SELECTORS = [
    "button[type='submit']",
    "button[aria-label*='send' i]",
    "button[aria-label*='submit' i]",
]

RESPONSE_SELECTORS = [
    "main article",
    "[role='article']",
    "[data-testid*='assistant' i]",
    "[data-message-author-role='assistant']",
]


@dataclass
class CliConfig:
    prompt: str
    url: str
    headless: bool
    timeout_ms: int
    wait_s: int
    input_selectors: tuple
    send_selectors: tuple
    response_selectors: tuple
    storage_state: Optional[Path]
    user_data_dir: Optional[Path]
    save_storage_state: Optional[Path]
    dump_dir: Optional[Path]
    debug: bool


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return " ".join(args.prompt).strip()
    if args.prompt_file:
        return Path(args.prompt_file).read_text().strip()

    data = sys.stdin.read().strip()
    if data:
        return data
    raise SystemExit("prompt is required via argument, file, or stdin")


def _first_visible(page, selectors: Iterable[str]):
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()
        except Exception:
            continue
        for index in range(count):
            item = locator.nth(index)
            try:
                if item.is_visible():
                    return item, selector
            except Exception:
                continue
    return None, None


def _page_text(page, selector: str) -> str:
    try:
        return page.locator(selector).inner_text(timeout=1000).strip()
    except Exception:
        return ""


def _detect_blocker(page) -> str:
    title = ""
    body = ""
    content = ""
    try:
        title = page.title().strip()
    except Exception:
        pass
    try:
        body = page.locator("body").inner_text(timeout=1000).strip()
    except Exception:
        pass
    try:
        content = page.content()
    except Exception:
        pass

    blob = "\n".join([title, body, content]).lower()

    if "cloudfront" in blob or "403 error" in blob or "request blocked" in blob:
        return "DeepSeek returned a CloudFront 403 block page."
    if "captcha" in blob or "challenge" in blob:
        return "DeepSeek returned a bot challenge page."
    return ""


def _dump_artifacts(page, dump_dir: Path, label: str, reason: str) -> None:
    dump_dir.mkdir(parents=True, exist_ok=True)
    safe = label.replace(" ", "_").replace("/", "_").replace(":", "_")
    meta = {
        "url": "",
        "title": "",
        "reason": reason,
        "timestamp": int(time.time()),
    }
    try:
        meta["url"] = page.url
    except Exception:
        pass
    try:
        meta["title"] = page.title()
    except Exception:
        pass
    try:
        (dump_dir / f"{safe}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        page.screenshot(path=str(dump_dir / f"{safe}.png"), full_page=True)
    except Exception:
        pass
    (dump_dir / f"{safe}.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _wait_for_response(
    page, baseline: str, timeout_s: int, debug: bool, selectors: Iterable[str]
) -> str:
    deadline = time.time() + timeout_s
    last_text = ""

    while time.time() < deadline:
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = locator.count()
            except Exception:
                continue

            for index in range(count):
                node = locator.nth(index)
                try:
                    if not node.is_visible():
                        continue
                    text = node.inner_text(timeout=1000).strip()
                except Exception:
                    continue
                if text and text != baseline:
                    last_text = text

        if last_text:
            return last_text

        main_text = _page_text(page, "main")
        if main_text and main_text != baseline and len(main_text) > len(baseline):
            return main_text[len(baseline) :].strip() or main_text.strip()

        if debug:
            print("[debug] waiting for assistant response...", file=sys.stderr)
        page.wait_for_timeout(1000)

    raise TimeoutError("timed out waiting for DeepSeek response")


def _launch_context(pw, cfg: CliConfig) -> Tuple[object, Optional[object]]:
    if cfg.user_data_dir:
        context = pw.chromium.launch_persistent_context(
            str(cfg.user_data_dir),
            headless=cfg.headless,
            viewport={"width": 1440, "height": 1200},
        )
        return context, None

    browser = pw.chromium.launch(headless=cfg.headless)
    context_kwargs = {
        "viewport": {"width": 1440, "height": 1200},
    }
    if cfg.storage_state and cfg.storage_state.exists():
        context_kwargs["storage_state"] = str(cfg.storage_state)
    context = browser.new_context(**context_kwargs)
    return context, browser


def run(cfg: CliConfig) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise SystemExit(
            "playwright is not installed. Install it with:\n"
            "  python3 -m pip install --user playwright\n"
            "  python3 -m playwright install chromium"
        ) from exc

    with sync_playwright() as pw:
        context, browser = _launch_context(pw, cfg)
        try:
            page = context.new_page()
            try:
                page.goto(cfg.url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
                page.wait_for_timeout(2000)
            except Exception as exc:
                if cfg.dump_dir:
                    _dump_artifacts(page, cfg.dump_dir, "load_failure", str(exc))
                raise

            prompt_box, prompt_selector = _first_visible(page, cfg.input_selectors)
            if prompt_box is None:
                blocker = _detect_blocker(page)
                if blocker:
                    if cfg.dump_dir:
                        _dump_artifacts(page, cfg.dump_dir, "blocked_page", blocker)
                    raise SystemExit(blocker)
                if cfg.dump_dir:
                    _dump_artifacts(page, cfg.dump_dir, "no_prompt_input", "prompt input not found")
                raise SystemExit(
                    "could not find a prompt input. Use --debug or override the selector flags."
                )

            baseline = _page_text(page, "main")

            if cfg.debug:
                print(f"[debug] input selector: {prompt_selector}", file=sys.stderr)

            try:
                prompt_box.fill(cfg.prompt)
            except Exception:
                prompt_box.click()
                page.keyboard.type(cfg.prompt, delay=10)

            send_button, send_selector = _first_visible(page, cfg.send_selectors)
            if send_button is not None:
                if cfg.debug:
                    print(f"[debug] send selector: {send_selector}", file=sys.stderr)
                send_button.click()
            else:
                page.keyboard.press("Enter")

            try:
                response = _wait_for_response(
                    page, baseline, cfg.wait_s, cfg.debug, cfg.response_selectors
                )
            except Exception as exc:
                if cfg.dump_dir:
                    _dump_artifacts(page, cfg.dump_dir, "response_timeout", str(exc))
                raise

            if cfg.save_storage_state:
                cfg.save_storage_state.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(cfg.save_storage_state))

            return response
        finally:
            context.close()
            if browser is not None:
                browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek web chat CLI")
    parser.add_argument("prompt", nargs="*", help="Prompt text")
    parser.add_argument("--prompt-file", help="Read prompt text from a file")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    parser.add_argument("--wait-seconds", type=int, default=DEFAULT_WAIT_S)
    parser.add_argument("--storage-state", type=Path, default=DEFAULT_STORAGE_STATE)
    parser.add_argument("--user-data-dir", type=Path)
    parser.add_argument("--save-storage-state", type=Path)
    parser.add_argument("--dump-dir", type=Path)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--input-selector", action="append", dest="input_selectors")
    parser.add_argument("--send-selector", action="append", dest="send_selectors")
    parser.add_argument("--response-selector", action="append", dest="response_selectors")
    headless_group = parser.add_mutually_exclusive_group()
    headless_group.add_argument("--headless", action="store_true")
    headless_group.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    prompt = _read_prompt(args)
    cfg = CliConfig(
        prompt=prompt,
        url=args.url,
        headless=not args.headed,
        timeout_ms=args.timeout_ms,
        wait_s=args.wait_seconds,
        input_selectors=tuple(args.input_selectors or INPUT_SELECTORS),
        send_selectors=tuple(args.send_selectors or SEND_SELECTORS),
        response_selectors=tuple(args.response_selectors or RESPONSE_SELECTORS),
        storage_state=args.storage_state,
        user_data_dir=args.user_data_dir,
        save_storage_state=args.save_storage_state,
        dump_dir=args.dump_dir,
        debug=args.debug,
    )

    if cfg.user_data_dir and cfg.storage_state:
        print("--storage-state is ignored when --user-data-dir is set", file=sys.stderr)

    try:
        response = run(cfg)
    except Exception as exc:
        print(f"web_chat error: {exc}", file=sys.stderr)
        return 1

    print(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
