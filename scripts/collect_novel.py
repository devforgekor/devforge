#!/usr/bin/env python3
# Status: experimental
# Path: 사용자 직접 실행 (수동 수집 스크립트)
"""bookto31.com 하남자의 탑 공략법 전체 회차 수집.

8분 간격으로 FlareSolverr를 통해 Cloudflare 우회 후 수집.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

FLARESOLVERR_URL = "http://127.0.0.1:8191/v1"
EPISODE_IDS_FILE = Path("/opt/ai_data/flaresolverr/episode_ids.json")
OUTPUT_DIR = Path("/opt/ai_data/flaresolverr/novels/하남자의_탑_공략법")
RATE_LIMIT_INTERVAL = 480  # 8분
JITTER_MAX = 120  # ±2분

KST = timezone(timedelta(hours=9))


def load_episodes():
    with open(EPISODE_IDS_FILE) as f:
        return json.load(f)


def get_collected():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return {f.stem for f in OUTPUT_DIR.glob("*.json")}


def fetch_episode(wr_id: int, timeout: int = 60000) -> dict:
    url = f"https://bookto31.com/bbs/board.php?bo_table=novel&wr_id={wr_id}"
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": timeout,
    }

    with httpx.Client(timeout=timeout / 1000 + 30) as client:
        resp = client.post(FLARESOLVERR_URL, json=payload, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        data = resp.json()

    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr error: {data.get('message', 'unknown')}")

    sol = data.get("solution", {})
    html = sol.get("response", "")

    # 제목 추출
    title_match = re.search(r"<title>([^<]+)</title>", html)
    title = title_match.group(1).strip() if title_match else ""

    # 본문 추출 (book 스킨의 본문 영역)
    content_match = re.search(
        r'<div[^>]*class="[^"]*at-content[^"]*"[^>]*>(.*?)</div>\s*<!--',
        html, re.DOTALL
    )
    if not content_match:
        content_match = re.search(
            r'<div[^>]*class="[^"]*view-content[^"]*"[^>]*>(.*?)</div>',
            html, re.DOTALL
        )
    content = content_match.group(1).strip() if content_match else ""

    # HTML 태그 제거
    clean_content = re.sub(r"<[^>]+>", "", content)
    clean_content = re.sub(r"\s+", " ", clean_content).strip()

    return {
        "wr_id": wr_id,
        "title": title,
        "content_length": len(clean_content),
        "content": clean_content,
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
        print(f"  ⏳ {wait}초 대기 (다음 요청: {next_time.strftime('%H:%M:%S')})")
        time.sleep(wait)
    return wait


def main():
    import argparse

    parser = argparse.ArgumentParser(description="bookto31 소설 수집")
    parser.add_argument("--start", type=int, help="시작 wr_id (지정 시 해당 ID부터)")
    parser.add_argument("--dry-run", action="store_true", help="실제 수집 없이 목록만 확인")
    parser.add_argument("--limit", type=int, help="수집할 회차 수 제한")
    args = parser.parse_args()

    episodes = load_episodes()
    collected = get_collected()

    # wr_id 기준 정렬
    episodes_sorted = sorted(episodes, key=int)

    if args.start:
        episodes_sorted = [e for e in episodes_sorted if int(e) >= args.start]

    pending = [e for e in episodes_sorted if e not in collected]

    print(f"📖 하남자의 탑 공략법 수집")
    print(f"  전체 회차: {len(episodes)}개")
    print(f"  수집 완료: {len(collected)}개")
    print(f"  남은 회차: {len(pending)}개")
    print()

    if args.dry_run:
        print("📋 수집 대상 (dry-run):")
        for ep in pending[:20]:
            print(f"  wr_id={ep}")
        if len(pending) > 20:
            print(f"  ... 외 {len(pending)-20}개")
        return

    if args.limit:
        pending = pending[:args.limit]

    if not pending:
        print("✅ 모든 회차 수집 완료!")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    success = 0
    failed = 0

    for i, wr_id in enumerate(pending):
        print(f"\n[{i+1}/{len(pending)}] wr_id={wr_id} 수집 중...")

        try:
            result = fetch_episode(int(wr_id))
            outfile = OUTPUT_DIR / f"{wr_id}.json"
            with open(outfile, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

            print(f"  ✅ {result['title'][:40]} ({result['content_length']}자)")
            success += 1

        except Exception as e:
            print(f"  ❌ 실패: {e}")
            failed += 1

            # 실패 시 30분 대기 후 재시도
            if i < len(pending) - 1:
                print(f"  ⏳ 실패로 인한 30분 대기...")
                time.sleep(1800)
                continue

        # 다음 요청 전 대기
        if i < len(pending) - 1:
            wait_with_jitter(RATE_LIMIT_INTERVAL)

    print(f"\n{'='*50}")
    print(f"수집 완료: {success}개 성공, {failed}개 실패")
    print(f"저장 위치: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
