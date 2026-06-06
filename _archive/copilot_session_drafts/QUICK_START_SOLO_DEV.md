# ⚡ Day 1 Quickstart

```bash
# Copy templates from DEVFORGE_SIMPLE.md:
# 1. docker-compose.yml
# 2. requirements.txt  
# 3. Dockerfile
# 4. app/main.py
# 5. .github/dependabot.yml

# Then:
docker compose up

# Test (other terminal):
curl http://localhost:8000/health
# Should return: {"status":"ok"}
```

---

## 5-Point Checklist

- [ ] All `requirements.txt` versions use `==`
- [ ] `poetry.lock` or `package-lock.json` committed
- [ ] GitHub Repositories set to Watch → Releases
- [ ] `.github/dependabot.yml` created
- [ ] `docker compose up` works locally

---

Done! 🎉

→ Next: Week 1-2 implementation
