
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
