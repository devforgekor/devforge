#!/usr/bin/env python3
"""OCI instance launch retry — exponential backoff, max 5min reset, durable.
Sends Telegram notification on success."""

import json
import os
import subprocess
import sys
import time
import urllib.request

INSTANCE_NAME = "onmydoc"
LOG_FILE = f"/tmp/oci_launch_{INSTANCE_NAME}.log"
ENV = os.environ.copy()
ENV["SUPPRESS_LABEL_WARNING"] = "True"
ENV["PATH"] = "/home/opc/.local/bin:/usr/local/bin:/usr/bin:/bin"


def log(msg):
    t = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    line = f"[{t}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def get_secret(key):
    with open(os.path.expanduser("~/.config/devforge/secrets.env")) as f:
        for line in f:
            if line.startswith(key):
                return line.split("=", 1)[1].strip()
    return None


def send_telegram(text):
    token = get_secret("TELEGRAM_TOKEN")
    chat_id = get_secret("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("TELEGRAM: no token/chat_id configured")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": int(chat_id), "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log(f"TELEGRAM: sent ({resp.status})")
    except Exception as e:
        log(f"TELEGRAM: failed — {e}")


TENANCY_OCID = get_secret("OCI_TENANCY_OCID")
SUBNET_ID = (
    "ocid1.subnet.oc1.ap-tokyo-1.aaaaaaaan424htifcv4c3kjgqkwubtuxqaggmxguwx47gxtpu235oupy5jwq"
)
IMAGE_ID = "ocid1.image.oc1.ap-tokyo-1.aaaaaaaaswhlhdiaseviso3i6c5ozvxldsazmm7kbar6eetg6sijyaclohwa"
AD = "vXho:AP-TOKYO-1-AD-1"


def get_instance_public_ip(instance_id):
    try:
        result = subprocess.run(
            [
                "oci",
                "compute",
                "instance",
                "list-vnics",
                "--instance-id",
                instance_id,
                "--compartment-id",
                TENANCY_OCID,
                "--query",
                'data[0]."public-ip"',
                "--raw-output",
            ],
            capture_output=True,
            text=True,
            env=ENV,
            timeout=30,
        )
        ip = result.stdout.strip()
        return ip if ip and ip != "None" else "assigning..."
    except Exception as e:
        log(f"IP lookup failed: {e}")
        return "unknown"


def try_launch():
    cmd = [
        "oci",
        "compute",
        "instance",
        "launch",
        "--compartment-id",
        TENANCY_OCID,
        "--availability-domain",
        AD,
        "--display-name",
        INSTANCE_NAME,
        "--image-id",
        IMAGE_ID,
        "--shape",
        "VM.Standard.A1.Flex",
        "--shape-config",
        '{"ocpus": 2.0, "memory-in-gbs": 12.0}',
        "--subnet-id",
        SUBNET_ID,
        "--assign-public-ip",
        "true",
        "--ssh-authorized-keys-file",
        os.path.expanduser("~/.ssh/id_ed25519.pub"),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=ENV, timeout=120)
    except subprocess.TimeoutExpired:
        log("TIMEOUT — OCI API did not respond within 120s")
        return None
    if result.returncode == 0:
        try:
            data = json.loads(result.stdout) if result.stdout else {}
            instance_id = data.get("data", {}).get("id", "unknown")
            log(f"SUCCESS — instance {INSTANCE_NAME} created: {instance_id}")
            return instance_id
        except json.JSONDecodeError:
            log(f"SUCCESS — instance created. Output: {result.stdout[:200]}")
            return "created"
    else:
        err_msg = ""
        try:
            err_data = json.loads(result.stderr) if result.stderr else {}
            err_msg = err_data.get("message", "")
        except json.JSONDecodeError:
            pass
        log(f"FAIL — {err_msg or 'unknown error'}")
        return None


def main():
    log(f"Starting retry loop for instance '{INSTANCE_NAME}'")
    log(f"  Image: {IMAGE_ID}")
    log(f"  Subnet: {SUBNET_ID}")
    log("  Shape: VM.Standard.A1.Flex (2 OCPU, 12GB)")
    log(f"{'=' * 60}")

    delay = 2
    attempt = 0
    last_report = time.monotonic()  # 1-hour telegram heartbeat
    start_time = time.monotonic()

    send_telegram(
        f"🟢 OCI Tokyo 발사 시작\n"
        f"인스턴스: {INSTANCE_NAME}\n"
        f"스펙: VM.Standard.A1.Flex (2 OCPU, 12GB)\n"
        f"리전: AP-TOKYO-1\n"
        f"알림: 1시간 간격으로 경과 보고"
    )

    while True:
        attempt += 1
        log(f"Attempt {attempt} (delay={delay}s)...")
        instance_id = try_launch()
        if instance_id:
            log(f"Instance '{INSTANCE_NAME}' launched successfully!")
            public_ip = get_instance_public_ip(instance_id)
            tg_msg = (
                f"✅ OCI 인스턴스 생성 성공\n"
                f"보드: 인스턴스 {INSTANCE_NAME} 생성됨\n"
                f"이름: onmydoc\n"
                f"주소: {public_ip}\n"
                f"사용자: ubuntu\n"
                f"소요 시간: {int((time.monotonic() - start_time) / 60)}분\n"
                f"시도 횟수: {attempt}회\n"
                f"에더: 이 인스턴스는 Always Free 자격이 있으며, 현재 DEVFORGE에저 output 경로로 사용될 예정입니다."
            )
            send_telegram(tg_msg)
            sys.exit(0)

        delay *= 2
        if delay > 300:
            delay = 2

        # Hourly Telegram progress report
        elapsed = time.monotonic() - last_report
        if elapsed >= 3600:
            total_elapsed = int((time.monotonic() - start_time) / 60)
            send_telegram(
                f"⏳ OCI Tokyo 발사 진행 중\n"
                f"경과: {total_elapsed}분\n"
                f"시도 횟수: {attempt}회\n"
                f"마지막 오류: unknown error\n"
                f"다음 보고: 1시간 후"
            )
            last_report = time.monotonic()

        log(f"Waiting {delay}s before retry...")
        time.sleep(delay)


if __name__ == "__main__":
    main()
