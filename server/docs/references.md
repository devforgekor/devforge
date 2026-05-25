# References

LLM tools, frameworks, and research project references. For infrastructure dependencies, see `_archive/reference-watchlist.md`.

## Prompt Optimization & Ablation

- [DSPy](https://github.com/stanfordnlp/dspy) — Stanford NLP. MIPROv2 Bayesian instruction search, GEPA/COPRO. 22k+ stars.
- [LLMLingua](https://github.com/microsoft/LLMLingua) — Microsoft. Token-level compression via BERT distillation, 20x. 3.7k stars.
- [PromptBench](https://github.com/microsoft/promptbench) — Microsoft. Prompt sensitivity analysis, 7 adversarial levels × 22 datasets.
- [GEPA](https://github.com/gepa-ai/gepa) — Reflective evolutionary prompt optimization. Shopify, Databricks, Dropbox.
- [Token Budget Negotiator](https://github.com/dakshjain-1616/token-budget-negotiator) — Greedy section ablation + LLM judge. MCP server available.
- [Pruner](https://github.com/heikki-laitala/pruner) — tree-sitter code indexing, 15-62% cost reduction. No LLM needed.
- [Promptomatix](https://github.com/SalesforceAIResearch/promptomatix) — Salesforce. Cost-aware optimization, 40-50% length reduction.
- [hone](https://github.com/twaldin/hone) — CLI optimizer, Haiku 4.5: 55%→92% solve rate.

## Model Serving & Inference

- [llama.cpp](https://github.com/ggml-org/llama.cpp) — GGUF. DevForge primary runtime (32B IQ4_XS, 14B Q8_0).
- [Ollama](https://github.com/ollama/ollama) — llama.cpp wrapper with REST API (DevForge v2.x phase).

## Evaluation

- [SWE-bench](https://github.com/princeton-nlp/SWE-bench) — Princeton. Real GitHub issue resolution benchmark.
- [HumanEval](https://github.com/openai/human-eval) — OpenAI. Code generation (164 problems, pass@k).
- [LM Eval Harness](https://github.com/EleutherAI/lm-evaluation-harness) — EleutherAI. 200+ tasks.

## DevForge Stack

- [Podman](https://github.com/containers/podman) — Rootless containers.
- [Quadlet](https://github.com/containers/podlet) — systemd-generator for Podman.
- [Caddy](https://github.com/caddyserver/caddy) — Auto-HTTPS proxy.
- [asyncpg](https://github.com/MagicStack/asyncpg) — PostgreSQL async driver.
- [Tenacity](https://github.com/jd/tenacity) — Retry library.
