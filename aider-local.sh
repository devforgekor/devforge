#!/bin/bash
# aider with local DevForge LLM (qwen2.5-coder 7B)
export OPENAI_API_BASE=http://127.0.0.1:8080/v1
export OPENAI_API_KEY=unused
exec /opt/projects/aider-env/bin/aider --model openai/qwen2.5-coder-7b-instruct-q8_0.gguf "$@"
