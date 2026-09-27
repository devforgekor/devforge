#!/usr/bin/env python3.12
# Status: experimental
# Path: test for escalation_router
"""Quick test for EscalationRouter."""

import asyncio
import os

from src.routers.escalation_router import EscalationRouter

# Set required environment variables for testing
os.environ.setdefault("OPENROUTER_KEY_1", "test-key-1")
os.environ.setdefault("OPENROUTER_KEY_2", "test-key-2")
os.environ.setdefault("OPENROUTER_KEY_3", "test-key-3")
os.environ.setdefault("GITHUB_TOKEN", "test-github-token")
os.environ.setdefault("DEEPSEEK_API_KEY", "test-deepseek-key")

# Minimal test schema
TEST_SCHEMA = {
    "type": "object",
    "properties": {
        "result": {"type": "string"},
    },
    "required": ["result"],
}


async def test_free_tier() -> None:
    """Test OpenRouter free tier."""
    router = EscalationRouter()
    prompt = "Say 'hello world' in JSON format."

    try:
        result = await router.extract(prompt, TEST_SCHEMA, tier="free")
        print(f"✓ Free tier: {result}")
    except Exception as e:
        print(f"✗ Free tier failed: {e}")


async def test_premium_tier() -> None:
    """Test opencode Go subscription tier."""
    router = EscalationRouter()
    prompt = "Say 'hello world' in JSON format."

    try:
        result = await router.extract(prompt, TEST_SCHEMA, tier="premium")
        print(f"✓ Premium tier: {result}")
    except Exception as e:
        print(f"✗ Premium tier failed: {e}")


async def test_fallback_tier() -> None:
    """Test DeepSeek fallback tier."""
    router = EscalationRouter()
    prompt = "Say 'hello world' in JSON format."

    try:
        result = await router.extract(prompt, TEST_SCHEMA, tier="fallback")
        print(f"✓ Fallback tier: {result}")
    except Exception as e:
        print(f"✗ Fallback tier failed: {e}")


async def test_github_models_tier() -> None:
    """Test GitHub Models tier."""
    router = EscalationRouter()
    prompt = "Say 'hello world' in JSON format."

    try:
        result = await router.extract(prompt, TEST_SCHEMA, tier="github_models")
        print(f"✓ GitHub Models tier: {result}")
    except Exception as e:
        print(f"✗ GitHub Models tier failed: {e}")


async def test_auto_escalation() -> None:
    """Test automatic escalation logic (without real API calls)."""
    router = EscalationRouter()

    # Test simple prompt logic
    assert router._is_simple("Say 'hi' in JSON.")
    assert not router._is_simple("Extract all technical facts..." + "x" * 5000)

    # Test tier selection logic
    assert ("free" if router._is_simple("Say 'hi'") else "github_models") == "free"
    assert ("free" if router._is_simple("complex" * 1000) else "github_models") == "github_models"

    print("  Auto escalation logic: OK")


async def main() -> None:
    print("=== EscalationRouter Tests ===\n")

    print("1. Free Tier Test:")
    await test_free_tier()

    print("\n2. GitHub Models Tier Test:")
    await test_github_models_tier()

    print("\n3. Premium Tier Test:")
    await test_premium_tier()

    print("\n4. Fallback Tier Test:")
    await test_fallback_tier()

    print("\n5. Auto Escalation Test:")
    await test_auto_escalation()

    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())

