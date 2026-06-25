# Status: production
# Path: imported by — proxy_reviewer.py, night_cycle.sh

"""LLM API proxy modules for external model endpoints.

Each module wraps one external API: anthropic.py, gemini.py, search.py
(for web search), and shared utilities (auth, key rotation, cipher).
Accessed by proxy_reviewer.py during the nightly DeepSeek audit phase.
"""
