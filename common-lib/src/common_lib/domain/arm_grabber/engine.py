#!/usr/bin/env python3
"""domain/arm_grabber/engine | ARM grabber engine: parallel strategy orchestration, winner-takes-all, status reporting, orphan check | needs:oci,pytz | uses:domain/arm_grabber/strategy,domain/arm_grabber/notify,oci/compute,oci/config | config:arm-grabber.example.yaml | run_engine(),check_orphans(),status_reporter()"""

from __future__ import annotations

import threading
import logging
from datetime import datetime, timedelta
from typing import Any

import pytz
import oci

from oci.compute import get_public_ip, list_instances
from domain.arm_grabber.strategy import DirectStrategy, SmallResizeStrategy
from domain.arm_grabber.notify import send_email

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")


def check_orphans(compute_client: Any, compartment_id: str, active_set: set[str], active_lock: threading.Lock) -> list[str] | None:
    result = []
    try:
        instances = list_instances(compute_client, compartment_id)
        active_states = {
            "MOVING", "PROVISIONING", "RUNNING", "STARTING",
            "STOPPING", "STOPPED", "CREATING_IMAGE",
        }
        for inst in instances:
            with active_lock:
                if inst.id in active_set:
                    continue
            if inst.lifecycle_state in active_states:
                result.append(
                    f"  - {inst.id} [{inst.lifecycle_state}] {inst.display_name}"
                )
    except Exception as e:
        logger.error("orphan check error: %s", e)
        return None
    return result


def _run_status_reporter(oci_config: dict[str, str], compartment_id: str, active_set: set[str], active_lock: threading.Lock,
                         stats: dict[str, int], stats_lock: threading.Lock, notifier: dict[str, Any], done_event: threading.Event) -> None:
    while not done_event.is_set():
        now = datetime.now(KST)
        if now.hour < 9:
            next_report = now.replace(hour=9, minute=0, second=0, microsecond=0)
        elif now.hour < 21:
            next_report = now.replace(hour=21, minute=0, second=0, microsecond=0)
        else:
            next_report = now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)

        wait_sec = (next_report - now).total_seconds()
        logger.info("[REPORT] next: %s KST (%.1fh)",
                     next_report.strftime("%m/%d %H:%M"), wait_sec / 3600)
        if done_event.wait(wait_sec):
            return

        with stats_lock:
            body = (
                f"OCI ARM status ({next_report.strftime('%Y-%m-%d %H:%M')} KST)\n\n"
                f"1/6 launch OK: {stats['small_ok']}\n"
                f"resize fail: {stats['resize_fail']}\n"
                f"4/24 direct attempts: {stats['direct_attempts']}\n"
            )
            stats["small_ok"] = 0
            stats["resize_fail"] = 0
            stats["direct_attempts"] = 0

        client = oci.core.ComputeClient(oci_config)
        orphans = check_orphans(client, compartment_id, active_set, active_lock)
        if orphans is None:
            body += "\nOrphans: check failed"
        elif orphans:
            body += f"\nOrphans: {len(orphans)}\n" + "\n".join(orphans)
        else:
            body += "\nOrphans: 0"
        send_email("[OCI ARM] 12hr status", body,
                   notifier.get("smtp_user"), notifier.get("smtp_password"),
                   notifier.get("smtp_to"))


def run_engine(oci_cfg: dict[str, str], instance_cfg: dict[str, Any], timing: dict[str, Any] | None = None, notifier: dict[str, Any] | None = None) -> tuple[str | None, str | None]:
    timing = timing or {}
    notifier = notifier or {}

    done_event = threading.Event()
    winner_lock = threading.Lock()
    winner_info = {}
    active_set = set()
    active_lock = threading.Lock()
    stats = {"small_ok": 0, "resize_fail": 0, "direct_attempts": 0}
    stats_lock = threading.Lock()

    direct = DirectStrategy(
        oci_cfg, instance_cfg, timing, notifier,
        done_event, winner_lock, winner_info,
        active_set, active_lock, stats, stats_lock,
    )
    small = SmallResizeStrategy(
        oci_cfg, instance_cfg, timing, notifier,
        done_event, winner_lock, winner_info,
        active_set, active_lock, stats, stats_lock,
    )

    t_direct = threading.Thread(target=direct.run, daemon=True, name="direct")
    t_small = threading.Thread(target=small.run, daemon=True, name="small")
    threads = [t_direct, t_small]

    if notifier.get("status_report", True):
        t_status = threading.Thread(
            target=_run_status_reporter,
            args=(oci_cfg, instance_cfg["compartment_id"],
                  active_set, active_lock, stats, stats_lock,
                  notifier, done_event),
            daemon=True, name="status",
        )
        t_status.start()
        threads.append(t_status)

    t_direct.start()
    t_small.start()
    logger.info("[ENGINE] dual strategy started: direct 4/24 + small 1/6->resize")

    while not done_event.is_set():
        t_direct.join(timeout=1)
        t_small.join(timeout=1)
        if not t_direct.is_alive() and not t_small.is_alive():
            logger.error("both strategy threads exited")
            break

    if winner_info:
        inst_id = winner_info["instance_id"]
        strategy = winner_info["strategy"]
        client = winner_info["client"]
        public_ip = get_public_ip(client, instance_cfg["compartment_id"], inst_id)

        logger.info("winner: %s, instance: %s", strategy, inst_id)
        if public_ip:
            ssh_cmd = f"ssh -i /path/to/private_key opc@{public_ip}"
            logger.info("SSH: %s", ssh_cmd)
            body = (
                f"OCI ARM instance created successfully ({strategy})\n\n"
                f"OCID: {inst_id}\nPublic IP: {public_ip}\nSSH: {ssh_cmd}"
            )
            subject = f"[OCI ARM] Success! ({strategy})"
        else:
            logger.warning("no public IP found")
            body = (
                f"OCI ARM instance created successfully ({strategy})\n\n"
                f"OCID: {inst_id}\n(no public IP)"
            )
            subject = f"[OCI ARM] Success ({strategy}, no IP)"

        if notifier.get("on_success", True):
            send_email(subject, body,
                       notifier.get("smtp_user"), notifier.get("smtp_password"),
                       notifier.get("smtp_to"))
        return inst_id, public_ip

    return None, None
