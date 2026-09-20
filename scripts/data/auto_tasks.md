
## #8 [watchdog] 반복 실패: syssvc:netdata:down
## watchdog 반복 incident (자동 생성)

- dedup: `syssvc:netdata:down`
- symptom: inactive
- fail_count: 1
- recent action: - (-)

근본원인을 수정하고 테스트 후 PR 하세요. (watchdog_incidents 테이블 참고)

### context (masked)
```
$ systemctl show netdata -p Result,ExecMainStatus,NRestarts Result=success NRestarts=0 ExecMainStatus=0  $ journalctl -u netdata -n 40 Sep 11 03:42:21 devforge-444795 systemd[1]: Stopping Netdata, X-Ray Vision for your infrastructure!... Sep 11 03:42:22 devforge-444795 systemd[1]: netdata.service: Deactivated successfully. Sep 11 03:42:22 devforge-444795 systemd[1]: Stopped Netdata, X-Ray Vision for your infrastructure!. Sep 11 03:42:22 devforge-444795 systemd[1]: netdata.service: Consumed 1h 20min 4.832s CPU time. Sep 11 03:42:52 devforge-444795 systemd[1]: Started Netdata, X-Ray Vision for your infrastructure!. Sep 12 03:43:17 devforge-444795 systemd[1]: Stopping Netdata, X-Ray Vision for your infrastructure!... Sep 12 03:43:18 devforge-444795 systemd[1]: netdata.service: Deactivated successfully. Sep 12 03:43:18 devforge-444795 systemd[1]: Stopped Netdata, X-Ray Vision for your infrastructure!. Sep 12 03:43:18 devforge-444795 systemd[1]: netdata.service: Consumed 1h 18min 34.551s CPU time. Sep 12 03:43:48 devforge-444795 systemd[1]: Started Netdata, X-Ray Vision for your infrastructure!. Sep 13 03:30:21 devforge-444795 systemd[1]: Stopping Netdata, X-Ray Vision for your infrastructure!... Sep 13 03:30:22 devforge-444795 systemd[1]: netdata.service: Deactivated successfully. Sep 13 03:30:22 devforge-444795 systemd[1]: Stopped Netdata, X-Ray Vision for your infrastructure!. Sep 13 03:30:22 devforge-444795 systemd[1]: netdata.service: Consumed 1h 21min 23.154s CPU time. Sep 13 03:30:52 devforge-444795 systemd[1]: Started Netdata, X-Ray Vision for your infrastructure!. Sep 14 03:14:16 devforge-444795 systemd[1]: Stopping Netdata, X-Ray Vision for your infrastructure!... Sep 14 03:14:18 devforge-444795 systemd[1]: netdata.service: Deactivated successfully. Sep 14 03:14:18 devforge-444795 systemd[1]: Stopped Netdata, X-Ray Vision for your infrastructure!. Sep 14 03:14:18 devforge-444795 systemd[1]: netdata.service: Consumed 1h 20min 48.496s CPU time. 
```


---
GitHub Issue #8 — implement the description above.
Work autonomously. After implementation: git add -A && git commit -m "feat: resolve #8 [watchdog] 반복 실패: syssvc:netdata:down" && git push.
Then run: python3 cli.py dev pr 8

## #9 [watchdog] 반복 실패: svc:ebook-watcher:down
## watchdog 반복 incident (자동 생성)

- dedup: `svc:ebook-watcher:down`
- symptom: journal 조회 실패: Command '['journalctl', '--user', '-u', 'ebook-watcher', '--no-pager', '-n', '200']' timed out after 8 seconds
- fail_count: 1
- recent action: - (-)

근본원인을 수정하고 테스트 후 PR 하세요. (watchdog_incidents 테이블 참고)

### context (masked)
```
$ systemctl --user show ebook-watcher -p Result,ExecMainStatus,NRestarts Result=success NRestarts=0 ExecMainStatus=0  $ journalctl --user -u ebook-watcher -n 40 Sep 20 11:27:54 devforge-444795 kv-fetch-env.py[7921]: 2026-09-20 11:27:54,372 [INFO] Sep 20 11:27:54 devforge-444795 kv-fetch-env.py[7921]: --- Cycle 15354 --- Sep 20 11:27:54 devforge-444795 kv-fetch-env.py[7921]: 2026-09-20 11:27:54,374 [INFO] 큐 비어 있음 - 새 회차 대기 중 Sep 20 11:27:54 devforge-444795 kv-fetch-env.py[7921]: 2026-09-20 11:27:54,374 [INFO]   1초 대기... Sep 20 11:27:55 devforge-444795 kv-fetch-env.py[7921]: 2026-09-20 11:27:55,374 [INFO] Sep 20 11:27:55 devforge-444795 kv-fetch-env.py[7921]: --- Cycle 15355 --- Sep 20 11:27:55 devforge-444795 kv-fetch-env.py[7921]: 2026-09-20 11:27:55,376 [INFO] 큐 비어 있음 - 새 회차 대기 중 Sep 20 11:27:55 devforge-444795 kv-fetch-env.py[7921]: 2026-09-20 11:27:55,376 [INFO]   1초 대기... Sep 20 11:27:56 devforge-444795 kv-fetch-env.py[7921]: 2026-09-20 11:27:56,377 [INFO] Sep 20 11:27:56 devforge-444795 kv-fetch-env.py[7921]: --- Cycle 15356 --- Sep 20 11:27:56 devforge-444795 kv-fetch-env.py[7921]: 2026-09-20 11:27:56,380 [INFO] 큐 비어 있음 - 새 회차 대기 중 Sep 20 11:27:56 devforge-444795 kv-fetch-env.py[7921]: 2026-09-20 11:27:56,380 [INFO]   1초 대기... Sep 20 11:27:57 devforge-444795 kv-fetch-env.py[7921]: 2026-09-20 11:27:57,380 [INFO] Sep 20 11:27:57 devforge-444795 kv-fetch-env.py[7921]: --- Cycle 15357 --- Sep 20 11:27:57 devforge-444795 kv-fetch-env.py[7921]: 2026-09-20 11:27:57,382 [INFO] 큐 비어 있음 - 새 회차 대기 중 Sep 20 11:27:57 devforge-444795 kv-fetch-env.py[7921]: 2026-09-20 11:27:57,382 [INFO]   1초 대기... Sep 20 11:27:58 devforge-444795 kv-fetch-env.py[7921]: 2026-09-20 11:27:58,382 [INFO] Sep 20 11:27:58 devforge-444795 kv-fetch-env.py[7921]: --- Cycle 15358 --- Sep 20 11:27:58 devforge-444795 kv-fetch-env.py[7921]: 2026-09-20 11:27:58,384 [INFO] 큐 비어 있음 - 새 회차 대기 중 Sep 20 11:27:58 devforge-444795 kv-fetch-env.py[7921]: 2026-09-20 11:27:58,384 [INFO]   1초 대기... Sep 20 11:27:59 dev
```


---
GitHub Issue #9 — implement the description above.
Work autonomously. After implementation: git add -A && git commit -m "feat: resolve #9 [watchdog] 반복 실패: svc:ebook-watcher:down" && git push.
Then run: python3 cli.py dev pr 9
