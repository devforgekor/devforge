#!/usr/bin/env python3
# Status: experimental
# Path: 사용자 직접 실행 (수동 수집 스크립트)
"""bookto31.com 하남자의 탑 공략법 전체 회차 수집.

557화(1화~557화)를 8분 간격으로 FlareSolverr 통해 수집.
wr_id 범위: 21431(1화) ~ 21987(557화)
"""

import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

FLARESOLVERR_URL = "http://127.0.0.1:8191/v1"
OUTPUT_DIR = Path("/opt/ai_data/flaresolverr/novels/하남자의_탑_공략법")
RATE_LIMIT_INTERVAL = 480  # 8분
JITTER_MAX = 120  # ±2분
FIRST_WR_ID = 21431   # 1화
LAST_WR_ID = 21987    # 557화
TOTAL_EPISODES = 557

KST = timezone(timedelta(hours=9))


def get_collected():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return {f.stem for f in OUTPUT_DIR.glob("*.json")}


def fetch_episode(wr_id: int, timeout: int = 120000) -> dict:
    url = f"https://bookto31.com/bbs/board.php?bo_table=novel&wr_id={wr_id}"
    payload = {"cmd": "request.get", "url": url, "maxTimeout": timeout}

    with httpx.Client(timeout=timeout / 1000 + 30) as client:
        resp = client.post(FLARESOLVERR_URL, json=payload, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        data = resp.json()

    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr error: {data.get('message', 'unknown')}")

    sol = data.get("solution", {})
    html = sol.get("response", "")
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.string.strip() if soup.title else ""

    chapter_match = re.search(r"-\s*(\d+)화", title)
    chapter = int(chapter_match.group(1)) if chapter_match else None

    content = ""
    vc = soup.find(class_="view-content")
    if vc:
        inner = vc.find("div")
        if inner:
            for tag in inner.find_all(["script", "style"]):
                tag.decompose()
            content = inner.get_text(separator="\n", strip=True)

    return {
        "wr_id": wr_id,
        "chapter": chapter,
        "title": title,
        "content_length": len(content),
        "content": content,
        "url": url,
        "collected_at": datetime.now(KST).isoformat(),
        "user_agent": sol.get("userAgent", ""),
    }


def wait_with_jitter(seconds: int) -> int:
    import random
    jitter = random.randint(-JITTER_MAX, JITTER_MAX)
    wait = max(0, seconds + jitter)
    if wait > 0:
        next_time = datetime.now(KST) + timedelta(seconds=wait)
        print(f"  ... 대기 {wait}초 (다음: {next_time.strftime('%H:%M:%S')})")
        time.sleep(wait)
    return wait


def main():
    import argparse

    parser = argparse.ArgumentParser(description="bookto31 소설 수집")
    parser.add_argument("--start", type=int, help="시작 회차 번호 (1~557)")
    parser.add_argument("--end", type=int, help="끝 회차 번호 (1~557)")
    parser.add_argument("--dry-run", action="store_true", help="수집 대상 목록만 확인")
    parser.add_argument("--limit", type=int, help="수집할 회차 수 제한")
    args = parser.parse_args()

    collected = get_collected()
    start_ch = args.start or 1
    end_ch = args.end or TOTAL_EPISODES

    all_wr_ids = []
    for ch in range(start_ch, end_ch + 1):
        wr_id = FIRST_WR_ID + (ch - 1)
        all_wr_ids.append((ch, wr_id))

    pending = [(ch, wid) for ch, wid in all_wr_ids if str(wid) not in collected]

    print(f"{'='*50}")
    print(f"  하남자의 탑 공략법 수집")
    print(f"  회차 범위: {start_ch}화 ~ {end_ch}화")
    print(f"  전체: {len(all_wr_ids)}화 | 수집됨: {len(all_wr_ids)-len(pending)}화 | 대기: {len(pending)}화")
    print(f"{'='*50}")

    if args.dry_run:
        print("\n수집 대상 (dry-run):")
        for ch, wid in pending[:20]:
            print(f"  {ch}화 (wr_id={wid})")
        if len(pending) > 20:
            print(f"  ... 외 {len(pending)-20}화")
        print(f"\n예상 소요 시간: ~{(len(pending) * (RATE_LIMIT_INTERVAL + 30)) / 3600:.1f}시간")
        return

    if args.limit:
        pending = pending[:args.limit]

    if not pending:
        print("\n모든 회차 수집 완료!")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    success = 0
    failed = 0
    failed_ids = []

    for i, (ch, wr_id) in enumerate(pending):
        ts = datetime.now(KST).strftime("%H:%M:%S")
        print(f"\n[{i+1}/{len(pending)}] {ch}화 (wr_id={wr_id}) [{ts}]")

        try:
            result = fetch_episode(wr_id)
            outfile = OUTPUT_DIR / f"{wr_id}.json"
            with open(outfile, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"  OK: {result['content_length']}자")
            success += 1

        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1
            failed_ids.append(wr_id)
            if i < len(pending) - 1:
                print(f"  30분 대기 후 재시도...")
                time.sleep(1800)

        if i < len(pending) - 1:
            wait_with_jitter(RATE_LIMIT_INTERVAL)

    print(f"\n{'='*50}")
    print(f"  완료: {success} 성공 / {failed} 실패")
    if failed_ids:
        print(f"  실패 ID: {failed_ids}")
    print(f"  저장: {OUTPUT_DIR}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
