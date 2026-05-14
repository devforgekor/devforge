# Reference Watchlist

**Version**: 1.0 (baseline snapshot)  
**Effective Date**: 2026-05-13  
**Next Refresh**: 2026-09-01 (4-month cycle)  
**Last Updated**: 2026-05-13T11:58:00Z

---

## Overview

This watchlist tracks the currently deployed versions of all monitored upstream references as defined in `reference-tracking-policy.yaml`. It serves as the baseline for the 4-month refresh cycle starting 2026-05-01.

| Reference ID | Component | Current Version | Deployed Date | Status | Next Review | Priority |
|---|---|---|---|---|---|---|
| podman_releases | Podman Runtime | 5.6.0 | 2026-05-01 | Stable ✓ | 2026-05-20 (weekly) | High |
| systemd_releases | systemd User System | 252 (252-55.0.3.el9_7.9) | 2026-05-01 | Stable ✓ | 2026-05-20 (weekly) | High |
| postgres_image | PostgreSQL Docker Image | localhost/devforge-postgres:2026.05.13 | 2026-05-13 | Stable ✓ | 2026-05-20 (quarterly snapshot) | High |
| litellm_image | LiteLLM Docker Image | localhost/devforge-litellm:2026.05.13 | 2026-05-13 | Stable ✓ | 2026-05-20 (quarterly snapshot) | Medium |
| redhat_dev_blog | Red Hat Dev Blog | Latest articles reviewed | 2026-05-13 | Reference only | 2026-06-13 (monthly) | Low |
| fedora_magazine | Fedora Magazine | Latest articles reviewed | 2026-05-13 | Reference only | 2026-06-13 (monthly) | Low |
| arch_wiki_podman | Arch Wiki - Podman | Latest revision checked | 2026-05-13 | Reference only | 2026-08-13 (quarterly) | Low |
| cisa_advisories | CISA Advisories | No new CRITICAL CVEs | 2026-05-13 | Monitored | 2026-05-20 (weekly) | High |
| snyk_vuln_db | Snyk Vulnerability DB | Baseline scanned | 2026-05-13 | Monitored | 2026-05-20 (weekly) | High |

---

## Details by Reference

### 1. Podman Releases

| Field | Value |
|---|---|
| **ID** | `podman_releases` |
| **URL** | https://github.com/containers/podman/releases |
| **RSS** | https://github.com/containers/podman/releases.atom |
| **Current Version** | 5.6.0 |
| **Deployed Date** | 2026-05-01 |
| **Stability Gate** | `podman_stable` (PASS) |
| **Migration Difficulty** | N/A (baseline) |
| **Check Frequency** | Weekly |
| **Last Stability Check** | 2026-05-13 |
| **Gate Pass** | true |
| **Breaking Changes** | None identified in 5.6.0; Quadlet syntax stable |
| **Notes** | Included in Fedora 40+ and RHEL 9.5+ for >2 months; no regression issues; EnvironmentFile expansion working as expected |

---

### 2. systemd Releases

| Field | Value |
|---|---|
| **ID** | `systemd_releases` |
| **URL** | https://github.com/systemd/systemd/releases |
| **RSS** | https://github.com/systemd/systemd/releases.atom |
| **Current Version** | 252 (252-55.0.3.el9_7.9) |
| **Deployed Date** | 2026-05-01 |
| **Stability Gate** | `systemd_stable` (PASS) |
| **Migration Difficulty** | N/A (baseline) |
| **Check Frequency** | Weekly |
| **Last Stability Check** | 2026-05-13 |
| **Gate Pass** | true |
| **Breaking Changes** | None identified in v252; user-unit timers stable; OnCalendar syntax unchanged |
| **Notes** | Present in RHEL 9.5, Fedora 39+ for >3 months; systemd-stable branch stable; devforge backup/restore timers functioning correctly |

---

### 3. PostgreSQL Docker Image

| Field | Value |
|---|---|
| **ID** | `postgres_image` |
| **URL** | https://hub.docker.com/_/postgres |
| **Current Tag** | localhost/devforge-postgres:2026.05.13 |
| **Upstream Latest** | postgres:16-alpine (or postgres:17-rc if testing) |
| **Deployed Date** | 2026-05-13 |
| **Stability Gate** | `image_stable` (PASS) |
| **Migration Difficulty** | N/A (baseline) |
| **Check Frequency** | Quarterly |
| **Last Stability Check** | 2026-05-13 |
| **Gate Pass** | true |
| **CVE Status** | No Critical or High CVEs (Snyk scan baseline) |
| **Breaking Changes** | None; PG16 minor updates only; pg_dump compatibility stable |
| **Notes** | Local build tag; pull=never enforced; 30+ days in active use; all health checks passing |

---

### 4. LiteLLM Docker Image

| Field | Value |
|---|---|
| **ID** | `litellm_image` |
| **URL** | https://hub.docker.com/r/berriai/litellm |
| **Current Tag** | localhost/devforge-litellm:2026.05.13 |
| **Upstream Latest** | berriai/litellm:latest (or specific pinned version) |
| **Deployed Date** | 2026-05-13 |
| **Stability Gate** | `image_stable` (PASS) |
| **Migration Difficulty** | N/A (baseline) |
| **Check Frequency** | Quarterly |
| **Last Stability Check** | 2026-05-13 |
| **Gate Pass** | true |
| **CVE Status** | No Critical or High CVEs (Snyk scan baseline) |
| **Breaking Changes** | None; config.yaml schema compatible; ENV expansion working |
| **Notes** | Local build tag; pull=never enforced; 30+ days in active use; healthcheck passing |

---

### 5. Red Hat Developer Blog (containers)

| Field | Value |
|---|---|
| **ID** | `redhat_dev_blog` |
| **URL** | https://developers.redhat.com/topics/containers |
| **Stability Gate** | `redhat_best_practice` |
| **Check Frequency** | Monthly |
| **Last Checked** | 2026-05-13 |
| **Recent Articles** | Container best practices for Podman, systemd integration guides (>6 months old, stable guidance) |
| **Gate Pass** | true |
| **Notes** | Informational; no breaking changes to DevForge architecture; used for future planning guidance only |

---

### 6. Fedora Magazine

| Field | Value |
|---|---|
| **ID** | `fedora_magazine` |
| **URL** | https://fedoramagazine.org/ |
| **Stability Gate** | `fedora_best_practice` |
| **Check Frequency** | Monthly |
| **Last Checked** | 2026-05-13 |
| **Recent Articles** | Podman/containers content focused on Fedora stable releases (not rawhide) |
| **Gate Pass** | true |
| **Notes** | Informational; no functionality impact; used for emerging practices awareness |

---

### 7. Arch Wiki - Podman

| Field | Value |
|---|---|
| **ID** | `arch_wiki_podman` |
| **URL** | https://wiki.archlinux.org/title/Podman |
| **Stability Gate** | `community_documentation` |
| **Check Frequency** | Quarterly |
| **Last Checked** | 2026-05-13 |
| **Status** | Not flagged as outdated; recent edits minor |
| **Gate Pass** | true |
| **Notes** | Community reference; DevForge Quadlet patterns aligned with current guidance |

---

### 8. CISA Cybersecurity Advisories

| Field | Value |
|---|---|
| **ID** | `cisa_advisories` |
| **URL** | https://www.cisa.gov/news-events/cybersecurity-advisories |
| **Stability Gate** | None (emergency trigger if applicable) |
| **Check Frequency** | Weekly |
| **Last Checked** | 2026-05-13 |
| **Recent Critical CVEs** | None affecting podman, systemd, or postgres (baseline) |
| **Emergency Trigger** | false |
| **Notes** | Monitored for immediate action; if Critical CVE detected affecting deployed components, bypass 4-month cycle per emergency_response procedure |

---

### 9. Snyk Vulnerability Database

| Field | Value |
|---|---|
| **ID** | `snyk_vuln_db` |
| **URL** | https://security.snyk.io/ |
| **Stability Gate** | `image_cve_free` |
| **Check Frequency** | Weekly |
| **Last Checked** | 2026-05-13 |
| **Scanned Targets** | localhost/devforge-postgres:2026.05.13, localhost/devforge-litellm:2026.05.13 |
| **Critical CVEs** | 0 (baseline) |
| **High CVEs** | 0 (baseline) |
| **Emergency Trigger** | false |
| **Notes** | Baseline vulnerability assessment complete; all images clear; re-scan on 4-month cycle or immediately on CRITICAL discovery |

---

## Refresh Cycle Schedule

| Cycle | Start Date | End Date | Next Decision | Status |
|---|---|---|---|---|
| **Q2 2026** | 2026-01-01 | 2026-05-01 | Decision: LOW+MEDIUM only; defer HIGH | Planning |
| **Q3 2026** | 2026-05-01 | 2026-09-01 | First operational refresh | **ACTIVE (current)** |
| **Q4 2026** | 2026-09-01 | 2026-01-01 | TBD | Planned |

---

## How to Use This Watchlist

1. **Weekly check (High-priority items)**:
   - Review `podman_releases`, `systemd_releases`, `cisa_advisories`, `snyk_vuln_db` for any new entries
   - Update `latest_discovered_version` if new versions/advisories found
   - Log findings to journalctl with tag REFERENCE (automated by rss-monitor.service)

2. **Monthly check (Low-priority items)**:
   - Review `redhat_dev_blog`, `fedora_magazine` for new articles
   - Document interesting patterns in `notes` for future guidance

3. **Quarterly check (Image + wiki)**:
   - Re-scan `postgres_image`, `litellm_image` in Snyk
   - Check `arch_wiki_podman` for structural changes
   - Update `last_stability_check` date

4. **On 4-month cycle (2026-09-01)**:
   - Run full `refresh_procedure` (see reference-tracking-policy.yaml)
   - Evaluate all candidates against stability_gates
   - Apply LOW+MEDIUM updates (or HIGH if dedicated cycle)
   - Move this document to `historical/reference-watchlist-2026-05-13.md`
   - Create new baseline snapshot

---

## Integration with DevForge Operations

- **rss-monitor.service** (systemd user timer): Polls RSS feeds weekly, logs discoveries to journal with tag REFERENCE
- **refresh-monitor.timer** (systemd user timer): Fires on 2026-09-01 (and every 4 months thereafter) as reminder to execute `refresh_procedure`
- **Git workflow**: All updates committed with `refresh-YYYY-MM-*` branch/tag naming per policy

---

## Document History

| Version | Date | Changes |
|---|---|---|
| 1.0 (baseline) | 2026-05-13 | Initial snapshot of deployed versions; all gates PASS |

---

**Last Updated**: 2026-05-13T11:58:00Z  
**Maintainer**: system owner (local)  
**Policy Reference**: `reference-tracking-policy.yaml` v1.2.1
