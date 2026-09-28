# Web Chat CLI - Qwen and DeepSeek Integration

This document covers **two separate tools** that talk to the Qwen and DeepSeek
web UIs: the `webq`/`webd` shell functions (chrome-web-llm stack — the supported
entry point) and `web_chat.py` (a standalone Playwright driver).

## Features

### 1. Dual Platform Support
- **Qwen**: Access Qwen's web chat interface
- **DeepSeek**: Access DeepSeek's web chat interface

### 2. Account Management
- Separate browser storage per platform (`--storage-state` / `--user-data-dir`)
- Login form auto-fill driven by `<ACCOUNT>_USERNAME/PASSWORD` **environment
  variables** — there is no Key Vault wiring in this script
- Because nothing injects those variables here, the script runs logged-out
  (see the caveat under *Azure Key Vault Configuration*)

### 3. Shared State and Content Sharing
- **Cross-platform content sharing**: Prompts and responses can be shared between accounts
- **Session management**: Track interactions across different accounts
- **State persistence**: Maintain shared context between account switches

### 4. Entry points

Two independent ways to drive Qwen/DeepSeek. `webq`/`webd` do **not** use this
script.

| Command | Implementation | Requirement |
|---------|----------------|-------------|
| `webq` / `webd` | shell functions in `~/.bashrc` → `web-llm.sh` (chrome-web-llm stack) | `stack-start.sh` running |
| `webqwen` / `webdeepseek` | removed (they were aliases to this script and shadowed the functions) | — |
| `python3.12 web_chat.py` | this script (Playwright) | `playwright install chromium` |

`webq` defaults to Qwen + web search; `webd` defaults to DeepSeek + web search
+ DeepThink. See `docs/runbooks/web-llm.md`.

## Installation and Setup

### Prerequisites

`webq`/`webd` (shell functions) — one-time per boot:
```bash
~/.local/share/chrome-web-llm/scripts/stack-start.sh   # relay :9876 + Chromium + extension
```

`web_chat.py` (this script):
```bash
python3.12 -m pip install playwright
python3.12 -m playwright install chromium
```
> Use `python3.12`, not `python3`. On this host `python3` is 3.9 with a
> different Playwright build, so its browser revision does not match the
> installed Chromium.

### Azure Key Vault Configuration
Set up Azure Key Vault secrets for both accounts:
- `DEEPSEEK-AI-ACCOUNT` (JSON format: {"username": "value", "password": "value"})
- `QWEN-AI-ACCOUNT` (JSON format: {"username": "value", "password": "value"})

> **Not provisioned in this environment.** `web_chat.py` only looks for these
> `<ACCOUNT>_*USERNAME/PASSWORD` environment variables, and no wrapper injects
> them — so it always runs a **logged-out** browser context. The `webq`/`webd`
> shell functions instead reuse the logged-in `chrome-web-llm-profile`.

### Environment Variables
Set the following environment variables for authentication:
```bash
export DEEPSEEK-AI-ACCOUNT_USERNAME="your_username"
export DEEPSEEK-AI-ACCOUNT_PASSWORD="your_password"
export QWEN-AI-ACCOUNT_USERNAME="your_username"
export QWEN-AI-ACCOUNT_PASSWORD="your_password"
```

## Usage

### Basic Usage — shell functions (`webq` / `webd`)

```bash
~/.local/share/chrome-web-llm/scripts/stack-start.sh   # one-time per boot (relay + Chromium)
webq "Hello, Qwen!"                             # Qwen + web search
webd "Hello, DeepSeek!"                         # DeepSeek + search + DeepThink
webq                                             # no args → "qwen[chat]> " prompt
WL_SESSION=proj webq "Hello, Qwen!"              # session proj
echo "Hello" | webq                              # read the question from stdin
```

### Basic Usage — this script (direct invocation)

```bash
python3.12 /opt/projects/server/scripts/web_chat.py --account qwen "Hello, Qwen!"
python3.12 /opt/projects/server/scripts/web_chat.py --account deepseek "Hello, DeepSeek!"
```

### Advanced Options (web_chat.py only)

```bash
# Save login state
web_chat.py --account deepseek --save-storage-state ~/deepseek_state.json "Save login state"

# Debug mode
web_chat.py --account qwen --debug "Enable debug output"

# Interactive prompt (no argument, TTY)
web_chat.py --account qwen
```

### Content Sharing Features
1. **Cross-account prompt sharing**: When switching accounts, prompts are shared with the previous account
2. **Response tracking**: Both platforms track and store responses
3. **Session continuity**: Shared state maintains context across account switches

## State Management

### Shared State
The following state is shared between accounts:
- **Previous responses**: Track responses from each platform
- **Cross-account prompts**: Store prompts that were shared between accounts
- **Switch history**: Track account switching patterns
- **Interaction timestamps**: Track when interactions occurred

### Storage
- User data directories: `~/.cache/devforge/qwen-profile` and `~/.cache/devforge/deepseek-profile`
  — created only when you pass `--user-data-dir`; by default `web_chat.py`
  launches an **ephemeral** context
- Storage states: `~/.config/devforge/qwen-storage-state.json` and `~/.config/devforge/deepseek-storage-state.json`
  — read on start, written only with `--save-storage-state` (neither exists yet)

## Examples

### Switching Between Accounts
```bash
# Start with Qwen
webq "Hello, this is my first message to Qwen."

# Switch to DeepSeek (prompt is shared)
webd "I'm switching to DeepSeek. My previous message was shared."

# Switch back to Qwen
webq "Back to Qwen. I received a response from DeepSeek earlier."
```

### Using Different Prompts
```bash
# Qwen-specific request
webq "Explain quantum computing to me in simple terms."

# DeepSeek-specific request (with shared context)
webd "Can you elaborate on that quantum computing explanation?"
```

### Session Management (shell functions)

`--shared` reads **and** writes the session file, so Qwen and DeepSeek hand the
conversation to each other automatically:

```bash
webq "Explain quantum computing simply."     # saved to conversations/chat.jsonl
webd "Can you elaborate on that?"            # reads chat.jsonl, writes it back
WL_SESSION=proj webq "Start a new topic."    # separate session
```

Session files: `~/.local/share/chrome-web-llm/conversations/<name>.jsonl`.

### Session Management (web_chat.py — browser storage state)

```bash
web_chat.py --account qwen --save-storage-state ~/qwen_state.json "my prompt"
# Later...
web_chat.py --account qwen --storage-state ~/qwen_state.json "Continue from saved session"
```

## Troubleshooting

### Common Issues
1. **`webq`/`webd` do nothing or fail to reach the relay**: start the stack first —
   `~/.local/share/chrome-web-llm/scripts/stack-start.sh` (relay `127.0.0.1:9876` + Chromium)
2. **Silent exit from `web-llm.sh`**: run it directly to see the Azure Key Vault error —
   deleted KV keys in `KEYS` make `kv-fetch-env.py` exit non-zero under `set -e`
3. **web_chat.py: browser executable missing**: `python3.12 -m playwright install chromium`
   (must match the interpreter — `python3` is 3.9 and has a different browser revision)
4. **web_chat.py: no answer / wrong text**: the script runs a fresh, logged-out
   browser context; prefer the `webq`/`webd` shell functions, which use the
   logged-in `chrome-web-llm-profile`

### Debug Mode
```bash
web_chat.py --account qwen --debug "my prompt"     # this script
DEBUG=1 ~/.local/share/chrome-web-llm/scripts/web-llm-cli.sh "my prompt"   # shell path
```

## File Structure

```
/opt/projects/server/scripts/
└── web_chat.py                          # this script (direct Playwright driver)

# Chrome state lives under $HOME, not next to the script:
~/.cache/devforge/qwen-profile/          # Playwright persistent profile (--user-data-dir)
~/.cache/devforge/deepseek-profile/
~/.config/devforge/qwen-storage-state.json      # --storage-state / --save-storage-state
~/.config/devforge/deepseek-storage-state.json

# webq/webd stack (shell functions):
~/.local/share/chrome-web-llm/           # relay + extension + web-llm.sh
~/.cache/devforge/chrome-web-llm-profile/  # logged-in profile (415MB)
~/.local/share/chrome-web-llm/conversations/<session>.jsonl
```

## Future Enhancements

- Multi-modal interaction support
- Custom prompt templates
- Automated conversation continuity
- Cross-platform conversation history
- Advanced content sharing features

## License

This script is provided as an experimental tool for interacting with Qwen and DeepSeek web interfaces.
Use responsibly and respect the terms of service of both platforms.
