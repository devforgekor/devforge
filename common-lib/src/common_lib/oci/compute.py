#!/usr/bin/env python3
"""oci/compute | OCI compute ops: launch, terminate, resize, wait state, get IP, list instances | needs:oci | launch_instance(),terminate_instance(),resize_instance(),wait_for_state(),wait_for_instance_running(),get_public_ip(),list_instances()"""

from __future__ import annotations

import time
from typing import Any

import oci


def wait_for_state(compute_client: Any, instance_id: str, target_states: set[str], timeout: int = 900, interval: int = 10) -> tuple[bool, str, list[str]]:
    start = time.time()
    state = "UNKNOWN"
    state_log = []
    while time.time() - start < timeout:
        instance = compute_client.get_instance(instance_id).data
        state = instance.lifecycle_state
        elapsed = int(time.time() - start)
        state_log.append(f"[{elapsed}s] {state}")
        if state in target_states:
            return True, state, state_log
        if state in ("TERMINATED", "TERMINATING"):
            return False, state, state_log
        time.sleep(interval)
    return False, state, state_log


def wait_for_instance_running(compute_client: Any, instance_id: str, timeout: int = 900, interval: int = 10) -> tuple[bool, str, list[str]]:
    return wait_for_state(compute_client, instance_id, {"RUNNING"}, timeout, interval)


def launch_instance(compute_client: Any, compartment_id: str, availability_domain: str,
                    display_name: str, subnet_id: str, image_id: str, ssh_authorized_key: str,
                    ocpus: int, memory_gb: int, boot_volume_size: int = 50) -> str:
    details = oci.core.models.LaunchInstanceDetails(
        compartment_id=compartment_id,
        availability_domain=availability_domain,
        display_name=display_name,
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=ocpus, memory_in_gbs=memory_gb
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id, boot_volume_size_in_gbs=boot_volume_size
        ),
        subnet_id=subnet_id,
        metadata={"ssh_authorized_keys": ssh_authorized_key},
    )
    response = compute_client.launch_instance(details)
    return response.data.id


def terminate_instance(compute_client: Any, instance_id: str) -> None:
    try:
        compute_client.terminate_instance(instance_id)
    except Exception:
        pass


def resize_instance(compute_client: Any, instance_id: str, target_ocpus: int, target_memory: int) -> bool:
    compute_client.instance_action(instance_id, "STOP")
    ok, _, _ = wait_for_state(compute_client, instance_id, {"STOPPED"}, timeout=300)
    if not ok:
        return False

    update = oci.core.models.UpdateInstanceDetails(
        shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(
            ocpus=target_ocpus, memory_in_gbs=target_memory
        )
    )
    try:
        compute_client.update_instance(instance_id, update)
    except Exception:
        try:
            compute_client.instance_action(instance_id, "START")
            wait_for_state(compute_client, instance_id, {"RUNNING"}, timeout=120)
        except Exception:
            pass
        return False

    compute_client.instance_action(instance_id, "START")
    ok, _, _ = wait_for_state(compute_client, instance_id, {"RUNNING"}, timeout=300)
    return ok


def get_public_ip(compute_client: Any, compartment_id: str, instance_id: str) -> str | None:
    try:
        vnics = compute_client.list_vnic_attachments(
            compartment_id=compartment_id, instance_id=instance_id
        ).data
        if not vnics:
            return None
        network_client = oci.core.VirtualNetworkClient(compute_client.config)
        vnic = network_client.get_vnic(vnics[0].vnic_id).data
        return vnic.public_ip
    except Exception:
        return None


def list_instances(compute_client: Any, compartment_id: str) -> list[Any]:
    return compute_client.list_instances(compartment_id=compartment_id).data
