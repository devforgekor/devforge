#!/usr/bin/env python3
# Status: experimental
# Path: devforge-worker — supervisor for raw_consumer + scheduled pipeline tasks
"""Worker container supervisor.

Runs:
  - raw_consumer (Pass 2): continuous loop, polls raw turns, cleans → pending
  - raw_consumer backfill (Pass 3): periodic oldest-first cleanup
  - Future: day_cycle / night_cycle scheduling
"""

import logging
import time

from pipelines.raw_consumer import process_raw_turns

POLL_INTERVAL = 30  # Pass 2: seconds between polls
BACKFILL_INTERVAL = 3600  # Pass 3: how often to run oldest-first (seconds)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("worker")


def _run_pass2():
    """Continuous Pass 2 loop — poll raw turns, newest-first."""
    last_backfill = time.monotonic()
    while True:
        try:
            n = process_raw_turns(backfill=False)
            if n > 0:
                logger.info("Pass 2: %d raw turns → pending", n)
        except Exception as e:
            logger.error("Pass 2 error: %s", e)

        # Periodic backfill check
        elapsed = time.monotonic() - last_backfill
        if elapsed >= BACKFILL_INTERVAL:
            try:
                nb = process_raw_turns(backfill=True)
                if nb > 0:
                    logger.info("Pass 3 backfill: %d raw turns → pending", nb)
            except Exception as e:
                logger.error("Pass 3 backfill error: %s", e)
            last_backfill = time.monotonic()

        time.sleep(POLL_INTERVAL)


def main():
    logger.info("Worker supervisor starting")
    logger.info("  Pass 2 poll interval: %ds (newest-first)", POLL_INTERVAL)
    logger.info("  Pass 3 backfill interval: %ds (oldest-first)", BACKFILL_INTERVAL)

    _run_pass2()


if __name__ == "__main__":
    main()
