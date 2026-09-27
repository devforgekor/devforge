#!/usr/bin/env python3.12
# Status: experimental
# Path: usage example for escalation_router
"""Example usage of EscalationRouter for extraction pipeline."""

import asyncio

from src.routers.escalation_router import EscalationRouter

# JSON Schema for structured extraction output
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence": {"type": "string"},
                },
                "required": ["subject", "predicate", "object", "confidence", "evidence"],
            },
        },
    },
    "required": ["facts"],
}


async def main() -> None:
    # Initialize router (reads keys from environment)
    router = EscalationRouter()

    # Example prompt
    prompt = """
    다음 대화에서 기술적 사실(fact)을 추출하세요:

    사용자: "시스템 메모리가 22Gi이고 4-core CPU입니다. 메모리 사용률이 60%를 넘으면 경고해줘."
    어시스턴트: "현재 메모리 22Gi 중 13.2Gi 사용 중(60%). 4-core CPU에서 메모리 대역폭이 병목입니다."
    """

    # 1. Automatic escalation (recommended)
    print("=== Auto Escalation ===")
    result = await router.extract(prompt, EXTRACTION_SCHEMA, tier="auto")
    print(result)

    # 2. Force free tier only
    print("\n=== Free Tier Only ===")
    result = await router.extract(prompt, EXTRACTION_SCHEMA, tier="free")
    print(result)

    # 3. Force premium (opencode Go subscription)
    print("\n=== Premium Tier ===")
    try:
        result = await router.extract(prompt, EXTRACTION_SCHEMA, tier="premium")
        print(result)
    except Exception as e:
        print(f"Premium failed: {e}")

    # 4. Force fallback (DeepSeek direct)
    print("\n=== Fallback Tier ===")
    try:
        result = await router.extract(prompt, EXTRACTION_SCHEMA, tier="fallback")
        print(result)
    except Exception as e:
        print(f"Fallback failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
