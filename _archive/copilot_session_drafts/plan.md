# DevForge: Personal AI Knowledge Base

**Mission**: 1인 개발, 4주 안에 작동하는 MCP 서버

---

## Architecture (1 Picture)

```
Chrome Extension (이미 완성)
        ↓
FastAPI Server + MCP (만들 것)
        ↓
PostgreSQL + pgvector
        ↓
Web UI / CLI / Claude Code
```

---

## Implementation (4 weeks)

- **Week 1-2**: PostgreSQL + FastAPI 기본 구조
- **Week 3**: Chrome Ext ↔ FastAPI 연동
- **Week 4**: 웹 UI + CLI 명령어

---

## Version Management (5 Rules = 90% 안정성)

1. **Exact versions** (`fastapi==0.104.1` not `^`)
2. **Lock files** (poetry.lock, package-lock.json)
3. **Watch releases** (GitHub → Releases)
4. **Dependabot** (monthly)
5. **Test locally** (docker compose up)

---

## Get Started

→ Read: `DEVFORGE_SIMPLE.md`

---

*Updated: 2026-05-13 13:01*
