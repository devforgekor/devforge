# Auto Tasks

<!-- Write tasks below using ## headings. One task per heading.
     Tasks execute via nightly_batch.sh Phase 4 (00:00 KST / 15:00 UTC).
     Each ## section = a separate Claude Code invocation.
     Full permissions granted. Results logged to auto_logs/.
     Leave empty (or only Example: headings) to skip.

## Handover: Restart container-devforge-swap to activate eviction
Restart container-devforge-swap to activate posix_fadvise page cache eviction.
Old supervisor binary is running without eviction support (handover known_issues).
Run: systemctl --user restart container-devforge-swap.service
Then verify: systemctl --user status container-devforge-swap.service
Check memory after restart: free -h

## Handover: Fix final report diff malformed
The synthesis JSON output in local_debate.py round_5_synthesis() is not properly serialized
to markdown diff format. final_report.md shows truncated/broken diff with JSON keys
appearing as diff lines. Root cause likely in write_report() function in debate_llm.py.
Review the code, find the bug, fix it.

## Handover: Investigate systemctl restart delay
systemctl --user restart --no-block can take 7+ minutes for the new container to start.
Root cause not determined — possible systemd rate limiting or Podman cleanup delays.
Investigate: check journalctl for the delay period, identify bottleneck, document findings.
--> 

