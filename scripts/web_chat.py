#!/usr/bin/env python3.12
# Status: production
# Path: none — library
"""web_chat.py — Qwen and DeepSeek web chat CLI via Playwright.

This is an isolated experimental tool for using the Qwen and DeepSeek web UI from a
server. It supports switching between Qwen and DeepSeek accounts with shared
state and content sharing capabilities. It is not wired into the existing session
ingestion pipeline.
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional, Tuple

# Import the Playwright API
from playwright.sync_api import sync_playwright

class AccountType(Enum):
    DEEPSEEK = "deepseek"
    QWEN = "qwen"

# Account configurations for each provider
ACCOUNT_CONFIGS = {
    AccountType.DEEPSEEK: {
        "url": "https://chat.deepseek.com/",
        "account_id": "DEEPSEEK-AI-ACCOUNT",
        "storage_state": Path.home() / ".config/devforge/deepseek-storage-state.json",
        "user_data_dir": Path.home() / ".cache/devforge/deepseek-profile",
    },
    AccountType.QWEN: {
        "url": "https://chat.qwen.ai/",
        "account_id": "QWEN-AI-ACCOUNT",
        "storage_state": Path.home() / ".config/devforge/qwen-storage-state.json",
        "user_data_dir": Path.home() / ".cache/devforge/qwen-profile",
    },
}

# Default account type
DEFAULT_ACCOUNT = AccountType.QWEN

DEFAULT_TIMEOUT_MS = 120000
DEFAULT_WAIT_S = 120

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
    account_type: AccountType


@dataclass
class SharedContext:
    """Shared context for switching between accounts."""
    current_account: AccountType
    previous_responses: dict[str, str]
    cross_account_prompts: list[str]
    shared_state: dict[str, any]
    
    def __init__(self):
        self.current_account = DEFAULT_ACCOUNT
        self.previous_responses = {}
        self.cross_account_prompts = []
        self.shared_state = {"last_interaction": time.time()}


class AccountManager:
    """Manage authentication and context switching between accounts."""
    
    def __init__(self):
        self.shared_context = SharedContext()
    
    def get_account_config(self, account_type: AccountType) -> dict:
        """Get account configuration for a specific account type."""
        return ACCOUNT_CONFIGS[account_type]
    
    def switch_account(self, new_account: AccountType, page) -> bool:
        """Switch to a different account and preserve shared state."""
        try:
            config = self.get_account_config(new_account)
            
            if new_account != self.shared_context.current_account:
                # Preserve cross-account state before switching
                self.shared_context.shared_state["switched_from"] = self.shared_context.current_account.value
                self.shared_context.shared_state["switched_to"] = new_account.value
                self.shared_context.shared_state["switch_time"] = time.time()
                
                # Navigate to the new account
                page.goto(config["url"], wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
                page.wait_for_timeout(3000)
                
                self.shared_context.current_account = new_account
                
                if self.shared_context.debug:
                    print(f"[debug] Switched from {self.shared_context.current_account.value} to {new_account.value}", file=sys.stderr)
                
                # Try to login to the new account
                if not _try_login(page, new_account, self.shared_context.debug):
                    return False
                    
                # Restore shared context
                self.shared_context.shared_state["last_account"] = self.shared_context.current_account.value
                return True
                
        except Exception as exc:
            if self.shared_context.debug:
                print(f"[debug] Failed to switch account: {exc}", file=sys.stderr)
            return False
    
    def share_prompt_with_previous_account(self, prompt: str) -> str:
        """Share a prompt with the previous account."""
        if self.shared_context.current_account != DEFAULT_ACCOUNT:
            previous_account = self.shared_context.current_account
            self.shared_context.cross_account_prompts.append(prompt)
            self.shared_context.shared_state["last_cross_prompt_time"] = time.time()
            
            if self.shared_context.debug:
                print(f"[debug] Prompt shared with previous account ({previous_account.value})", file=sys.stderr)
        
        return prompt
    
    def add_previous_response(self, response: str, account_type: AccountType):
        """Store response for later retrieval."""
        self.shared_context.previous_responses[account_type.value] = response
        self.shared_context.shared_state["last_response_time"] = time.time()
        self.shared_context.shared_state["last_response_account"] = account_type.value


def _read_prompt(args: argparse.Namespace, account_type: AccountType) -> str:
    if args.prompt:
        return " ".join(args.prompt).strip()
    if args.prompt_file:
        return Path(args.prompt_file).read_text().strip()

    if sys.stdin.isatty():
        # TTY without a prompt argument: ask instead of blocking until EOF,
        # otherwise the CLI sits there with no output at all.
        try:
            typed = input(f"{account_type.value}> ")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("prompt is required via argument, file, or stdin") from None
        if typed.strip():
            return typed.strip()
        raise SystemExit("prompt is required via argument, file, or stdin")

    data = sys.stdin.read().strip()
    if data:
        return data
    raise SystemExit("prompt is required via argument, file, or stdin")


def _get_account_credentials(account_id: str) -> Tuple[str, str]:
    """Retrieve account credentials from Azure Key Vault using JSON format.
    
    Args:
        account_id: Account identifier (DEEPSEEK-AI-ACCOUNT or QWEN-AI-ACCOUNT)
        
    Returns:
        Tuple of (username, password) for the account
        
    Raises:
        SystemExit: If credentials cannot be retrieved
    """
    try:
        # Use environment variables for credential management
        # In production, these would be managed by kv-fetch-env.py
        username = os.environ.get(f"{account_id}_USERNAME")
        password = os.environ.get(f"{account_id}_PASSWORD")
        
        if not username or not password:
            raise SystemExit(f"Missing environment variables for {account_id}. Set {account_id}_USERNAME and {account_id}_PASSWORD")
        
        return username, password
        
    except Exception as e:
        raise SystemExit(f"Error retrieving credentials for {account_id}: {e}")


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

    raise TimeoutError("timed out waiting for response")


def _launch_context(pw, cfg: CliConfig) -> Tuple[object, Optional[object]]:
    config = ACCOUNT_CONFIGS[cfg.account_type]
    
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
    account_manager = AccountManager()
    
    with sync_playwright() as pw:
        context, browser = _launch_context(pw, cfg)
        try:
            config = ACCOUNT_CONFIGS[cfg.account_type]
            
            page = context.new_page()
            try:
                page.goto(config["url"], wait_until="domcontentloaded", timeout=cfg.timeout_ms)
                page.wait_for_timeout(3000)
            except Exception as exc:
                if cfg.dump_dir:
                    _dump_artifacts(page, cfg.dump_dir, "load_failure", str(exc))
                raise

            # Try to login to the specified account
            if not _try_login(page, cfg.account_type, cfg.debug):
                raise SystemExit(f"Failed to login to {cfg.account_type.value}")

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

            # Share prompt with previous account before sending
            shared_prompt = account_manager.share_prompt_with_previous_account(cfg.prompt)

            try:
                prompt_box.fill(shared_prompt)
            except Exception:
                prompt_box.click()
                page.keyboard.type(shared_prompt, delay=10)

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

            # Store response in shared context
            account_manager.add_previous_response(response, cfg.account_type)

            if cfg.save_storage_state:
                cfg.save_storage_state.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(cfg.save_storage_state))

            return response
        finally:
            context.close()
            if browser is not None:
                browser.close()


def _try_login(page, account_type: AccountType, debug: bool) -> bool:
    """Attempt to login using account credentials from Azure Key Vault."""
    try:
        config = ACCOUNT_CONFIGS[account_type]
        account_id = config["account_id"]
        
        # Try to find login form elements
        username_input = _first_visible(page, ["input[type='text']", "input[name='username']", "input[placeholder*='email' i]"])[0]
        password_input = _first_visible(page, ["input[type='password']"])[0]
        login_button = _first_visible(page, ["button[type='submit']", "button[aria-label*='login' i]", "button:contains('Sign in')"])[0]
        
        if not username_input or not password_input or not login_button:
            if debug:
                print(f"[debug] Login form not found for {account_type.value}, may already be authenticated", file=sys.stderr)
            return True  # Assume already authenticated
        
        # Get credentials from Azure Key Vault
        if debug:
            print(f"[debug] Retrieving {account_type.value} credentials", file=sys.stderr)
        
        username, password = _get_account_credentials(account_id)
        
        if debug:
            print(f"[debug] Logging in as user: {username} for {account_type.value}", file=sys.stderr)
        
        # Fill login form
        username_input.fill(username)
        password_input.fill(password)
        login_button.click()
        
        # Wait for login to complete
        page.wait_for_timeout(3000)
        
        # Check if login was successful by looking for user-specific elements
        if _first_visible(page, ["[data-testid*='user-menu']"]) or _first_visible(page, ["button[aria-label*='account' i]"]):
            if debug:
                print(f"[debug] Login for {account_type.value} successful", file=sys.stderr)
            return True
        else:
            if debug:
                print(f"[debug] Login for {account_type.value} may have failed - check for error messages", file=sys.stderr)
            return False
            
    except Exception as exc:
        if debug:
            print(f"[debug] Login attempt for {account_type.value} failed: {exc}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Qwen and DeepSeek web chat CLI")
    parser.add_argument("prompt", nargs="*", help="Prompt text")
    parser.add_argument("--prompt-file", help="Read prompt text from a file")
    parser.add_argument("--url", default=DEFAULT_ACCOUNT.value)
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    parser.add_argument("--wait-seconds", type=int, default=DEFAULT_WAIT_S)
    parser.add_argument("--storage-state", type=Path, default=ACCOUNT_CONFIGS[DEFAULT_ACCOUNT]["storage_state"])
    parser.add_argument("--user-data-dir", type=Path)
    parser.add_argument("--save-storage-state", type=Path)
    parser.add_argument("--dump-dir", type=Path)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--input-selector", action="append", dest="input_selectors")
    parser.add_argument("--send-selector", action="append", dest="send_selectors")
    parser.add_argument("--response-selector", action="append", dest="response_selectors")
    parser.add_argument("--account", type=str, choices=[a.value for a in AccountType],
                       default=None, help="Account to use (qwen or deepseek)")
    headless_group = parser.add_mutually_exclusive_group()
    headless_group.add_argument("--headless", action="store_true")
    headless_group.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    # --account is authoritative; --url is a legacy alias for the account name.
    account_type_str = (args.account or args.url or DEFAULT_ACCOUNT.value).lower()
    try:
        account_type = AccountType(account_type_str)
    except ValueError:
        print(f"Error: Invalid account type '{account_type_str}'. Use 'qwen' or 'deepseek'.", file=sys.stderr)
        return 1

    prompt = _read_prompt(args, account_type)
    cfg = CliConfig(
        prompt=prompt,
        url=ACCOUNT_CONFIGS[account_type]["url"],
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
        account_type=account_type,
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