# Auto Tasks

<!--
 Tasks execute via nightly_batch.sh Phase 4 (00:00 KST / 15:00 UTC).
 CLI: python3 cli.py auto add "title" "description"
 Each ## section = a separate Claude Code invocation.
 Full permissions granted. Results logged to auto_logs/.
-->

## T01: Cooperative debate E2E test with --reuse-vms, delete VMs after completion
1. Verify Pod A (:8080) DeepSeek-V2-Lite healthy: curl http://127.0.0.1:8080/health
2. Switch to debate mode if needed: tr debate
3. Run cooperative debate: python3 scripts/debate.py --mode cooperative --reuse-vms -q "File: scripts/azure_spot.py\nTask: Review error handling and resource cleanup"
4. Wait for debate completion. Record per-round Judge scores, consensus speed, total elapsed.
5. Compare with session 20260526T154843Z (baseline). Note regressions or improvements.
6. DELETE ALL 3 SPOT VMs: python3 scripts/azure_spot.py --delete-all (Qwen, Nemotron, Gemma). Confirm via az vm list.
7. Record worklog with tags: cooperative,benchmark,t01

CRITICAL: After debate completes, IMMEDIATELY delete all 3 Azure Spot VMs to stop billing. Do not leave VMs running.

## Handover: Restart container-devforge-swap to activate eviction
Restart container-devforge-swap to activate posix_fadvise page cache eviction. Old supervisor binary is running without eviction support. Run: systemctl --user restart container-devforge-swap.service. Verify: systemctl --user status container-devforge-swap.service. Check memory after restart: free -h

## Handover: Fix final report diff malformed
The synthesis JSON output in local_debate.py round_5_synthesis() is not properly serialized to markdown diff format. final_report.md shows truncated/broken diff with JSON keys appearing as diff lines. Root cause likely in write_report() function in debate_llm.py. Review, find the bug, fix it.

## Handover: Investigate systemctl restart delay
systemctl --user restart --no-block can take 7+ minutes for the new container to start. Root cause not determined. Investigate: check journalctl for the delay period, identify bottleneck, document findings.
