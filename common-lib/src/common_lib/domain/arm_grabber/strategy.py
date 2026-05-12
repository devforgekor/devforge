#!/usr/bin/env python3
"""domain/arm_grabber/strategy | ARM grabber strategies: direct 4/24 launch, small 1/6->resize to 4/24 | needs:oci,pytz | uses:oci/compute,domain/arm_grabber/notify | config:arm-grabber.example.yaml | DirectStrategy(),SmallResizeStrategy()"""

from __future__ import annotations

import time
import threading
import logging
from datetime import datetime
from typing import Any

import pytz
import oci

from oci.compute import (
    launch_instance, terminate_instance, wait_for_instance_running, resize_instance
)
from domain.arm_grabber.notify import send_email

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")


def _get_wait_seconds(off_peak_hours: list[int] | None = None, off_peak_wait: int = 30, peak_wait: int = 300) -> int:
    hour = datetime.now(KST).hour
    off_peak = off_peak_hours or (0, 1, 2, 3, 4, 5)
    return off_peak_wait if hour in off_peak else peak_wait


def _now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


class DirectStrategy:
    def __init__(self, oci_config: dict[str, str], instance_config: dict[str, Any], timing: dict[str, Any], notifier: dict[str, Any],
                 done_event: threading.Event, winner_lock: threading.Lock, winner_info: dict[str, Any],
                 active_set: set[str], active_lock: threading.Lock, stats: dict[str, int], stats_lock: threading.Lock):
        self.oci_config = oci_config
        self.cfg = instance_config
        self.timing = timing
        self.notify = notifier
        self.done_event = done_event
        self.winner_lock = winner_lock
        self.winner_info = winner_info
        self.active_set = active_set
        self.active_lock = active_lock
        self.stats = stats
        self.stats_lock = stats_lock

    def run(self) -> None:
        client = oci.core.ComputeClient(self.oci_config)
        attempt = 0
        while not self.done_event.is_set():
            attempt += 1
            wait_sec = _get_wait_seconds(
                self.timing.get("off_peak_hours"),
                self.timing.get("off_peak_wait", 30),
                self.timing.get("peak_wait", 300),
            )
            logger.info("[%s] [DIRECT] attempt #%d (next wait: %ds)", _now_kst(), attempt, wait_sec)
            with self.stats_lock:
                self.stats["direct_attempts"] += 1

            instance_id = None
            try:
                instance_id = launch_instance(
                    client,
                    compartment_id=self.cfg["compartment_id"],
                    availability_domain=self.cfg["availability_domain"],
                    display_name=self.cfg.get("display_name", "devforge"),
                    subnet_id=self.cfg["subnet_id"],
                    image_id=self.cfg["image_id"],
                    ssh_authorized_key=self.cfg["ssh_authorized_key"],
                    ocpus=self.cfg["direct_ocpus"],
                    memory_gb=self.cfg["direct_memory_gb"],
                )
                self._track(instance_id)
                logger.info("[DIRECT] launched: %s", instance_id)

                ok, final_state, state_log = wait_for_instance_running(
                    client, instance_id, timeout=self.timing.get("direct_wait", 900)
                )
                if ok:
                    if self._claim_winner("direct", instance_id, client):
                        return
                    self._untrack(instance_id)
                    terminate_instance(client, instance_id)
                    return

                if final_state in ("TERMINATED", "TERMINATING"):
                    logger.warning("[DIRECT] %s, retry after cleanup", final_state)
                    self._untrack(instance_id)
                    terminate_instance(client, instance_id)
                    self._notify_failure("direct", instance_id, final_state, state_log)
                else:
                    logger.warning("[DIRECT] timeout (state=%s), retry", final_state)
                    self._untrack(instance_id)
                    terminate_instance(client, instance_id)

            except oci.exceptions.ServiceError as e:
                if "Out of" in str(e) or getattr(e, "status", None) == 429:
                    logger.warning("[DIRECT] capacity/limit: %s", e)
                else:
                    logger.error("[DIRECT] OCI error: %s", e)
                if instance_id:
                    self._untrack(instance_id)
                    terminate_instance(client, instance_id)

            except Exception as e:
                logger.error("[DIRECT] error: %s", e)
                if instance_id:
                    self._untrack(instance_id)
                    terminate_instance(client, instance_id)

            time.sleep(wait_sec)

    def _claim_winner(self, name: str, instance_id: str, client: Any) -> bool:
        with self.winner_lock:
            if not self.done_event.is_set():
                self.done_event.set()
                self.winner_info.update(
                    {"strategy": name, "instance_id": instance_id, "client": client}
                )
                return True
        return False

    def _track(self, instance_id: str) -> None:
        with self.active_lock:
            self.active_set.add(instance_id)

    def _untrack(self, instance_id: str) -> None:
        with self.active_lock:
            self.active_set.discard(instance_id)

    def _notify_failure(self, name: str, instance_id: str, state: str, state_log: list[str]) -> None:
        if not self.notify.get("on_failure", True):
            return
        body = (
            f"[{name}] ARM instance creation failed\n\n"
            f"Time: {_now_kst()} (KST)\n"
            f"OCID: {instance_id}\n"
            f"Final state: {state}\n\n"
            f"State log:\n" + "\n".join(state_log)
        )
        send_email(f"[OCI ARM] Creation failed - {state} ({name})", body,
                   self.notify.get("smtp_user"), self.notify.get("smtp_password"),
                   self.notify.get("smtp_to"))


class SmallResizeStrategy:
    def __init__(self, oci_config: dict[str, str], instance_config: dict[str, Any], timing: dict[str, Any], notifier: dict[str, Any],
                 done_event: threading.Event, winner_lock: threading.Lock, winner_info: dict[str, Any],
                 active_set: set[str], active_lock: threading.Lock, stats: dict[str, int], stats_lock: threading.Lock):
        self.oci_config = oci_config
        self.cfg = instance_config
        self.timing = timing
        self.notify = notifier
        self.done_event = done_event
        self.winner_lock = winner_lock
        self.winner_info = winner_info
        self.active_set = active_set
        self.active_lock = active_lock
        self.stats = stats
        self.stats_lock = stats_lock

    def run(self) -> None:
        client = oci.core.ComputeClient(self.oci_config)
        attempt = 0
        while not self.done_event.is_set():
            attempt += 1
            wait_sec = _get_wait_seconds(
                self.timing.get("off_peak_hours"),
                self.timing.get("off_peak_wait", 30),
                self.timing.get("peak_wait", 300),
            )
            logger.info("[%s] [SMALL] attempt #%d (next wait: %ds)", _now_kst(), attempt, wait_sec)

            instance_id = None
            try:
                instance_id = launch_instance(
                    client,
                    compartment_id=self.cfg["compartment_id"],
                    availability_domain=self.cfg["availability_domain"],
                    display_name=self.cfg.get("display_name", "devforge"),
                    subnet_id=self.cfg["subnet_id"],
                    image_id=self.cfg["image_id"],
                    ssh_authorized_key=self.cfg["ssh_authorized_key"],
                    ocpus=self.cfg["small_ocpus"],
                    memory_gb=self.cfg["small_memory_gb"],
                )
                self._track(instance_id)
                logger.info("[SMALL] launched 1/6: %s", instance_id)

                ok, final_state, state_log = wait_for_instance_running(
                    client, instance_id, timeout=self.timing.get("small_wait", 900)
                )
                if not ok:
                    if final_state in ("TERMINATED", "TERMINATING"):
                        logger.warning("[SMALL] %s, retry after cleanup", final_state)
                        self._untrack(instance_id)
                        terminate_instance(client, instance_id)
                        self._notify_failure("small", instance_id, final_state, state_log)
                    else:
                        logger.warning("[SMALL] timeout (state=%s), retry", final_state)
                        self._untrack(instance_id)
                        terminate_instance(client, instance_id)
                    time.sleep(wait_sec)
                    continue

                with self.stats_lock:
                    self.stats["small_ok"] += 1

                if self.done_event.is_set():
                    self._untrack(instance_id)
                    terminate_instance(client, instance_id)
                    return

                resize_retries = self.timing.get("resize_retries", 5)
                resize_delay = self.timing.get("resize_retry_delay", 60)
                resize_ok = False
                for retry in range(resize_retries):
                    if self.done_event.is_set():
                        self._untrack(instance_id)
                        terminate_instance(client, instance_id)
                        return
                    logger.info("[SMALL] resize attempt %d/%d", retry + 1, resize_retries)
                    if resize_instance(client, instance_id,
                                       self.cfg["target_ocpus"],
                                       self.cfg["target_memory_gb"]):
                        resize_ok = True
                        break
                    with self.stats_lock:
                        self.stats["resize_fail"] += 1
                    if retry < resize_retries - 1:
                        logger.info("[SMALL] resize failed, waiting %ds", resize_delay)
                        if self.done_event.wait(resize_delay):
                            self._untrack(instance_id)
                            terminate_instance(client, instance_id)
                            return

                if resize_ok:
                    if self._claim_winner("small->resize", instance_id, client):
                        return
                    self._untrack(instance_id)
                    terminate_instance(client, instance_id)
                    return
                else:
                    logger.warning("[SMALL] resize failed after %d attempts, retry", resize_retries)
                    self._untrack(instance_id)
                    terminate_instance(client, instance_id)
                    time.sleep(wait_sec)
                    continue

            except oci.exceptions.ServiceError as e:
                if "Out of" in str(e) or getattr(e, "status", None) == 429:
                    logger.warning("[SMALL] capacity/limit: %s", e)
                else:
                    logger.error("[SMALL] OCI error: %s", e)
                if instance_id:
                    self._untrack(instance_id)
                    terminate_instance(client, instance_id)
                time.sleep(wait_sec)
                continue

            except Exception as e:
                logger.error("[SMALL] error: %s", e)
                if instance_id:
                    self._untrack(instance_id)
                    terminate_instance(client, instance_id)
                time.sleep(wait_sec)
                continue

    def _claim_winner(self, name: str, instance_id: str, client: Any) -> bool:
        with self.winner_lock:
            if not self.done_event.is_set():
                self.done_event.set()
                self.winner_info.update(
                    {"strategy": name, "instance_id": instance_id, "client": client}
                )
                return True
        return False

    def _track(self, instance_id: str) -> None:
        with self.active_lock:
            self.active_set.add(instance_id)

    def _untrack(self, instance_id: str) -> None:
        with self.active_lock:
            self.active_set.discard(instance_id)

    def _notify_failure(self, name: str, instance_id: str, state: str, state_log: list[str]) -> None:
        if not self.notify.get("on_failure", True):
            return
        body = (
            f"[{name}] ARM instance creation failed\n\n"
            f"Time: {_now_kst()} (KST)\n"
            f"OCID: {instance_id}\n"
            f"Final state: {state}\n\n"
            f"State log:\n" + "\n".join(state_log)
        )
        send_email(f"[OCI ARM] Creation failed - {state} ({name})", body,
                   self.notify.get("smtp_user"), self.notify.get("smtp_password"),
                   self.notify.get("smtp_to"))
