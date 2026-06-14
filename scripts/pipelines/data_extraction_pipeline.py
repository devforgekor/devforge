# Status: production
#!/usr/bin/env python3
import sys
import os
from typing import Optional

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.extract import core, utils
from lib.common import log
from lib.db import advance_checkpoint

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    
    turns = core._get_unprocessed_turns(args.limit)
    log(f"Found {len(turns)} turns to process")
    
    last_created_at = None
    for turn in turns:
        if args.dry_run:
            log(f"Dry-run: processing turn {turn['id']}")
            continue
            
        # mapping text to assistant_text as expected by internal logic if needed
        facts = core._extract_facts(turn['user_turn'], turn['thinking'], turn['text'])
        verified = core._verify_extractions(facts, turn['user_turn'], turn['thinking'], turn['text'])
        
        for i, f in enumerate(verified):
            core._insert_fact(turn['id'], i, f.get('type', 'FACT'), f.get('evidence', ''), 'day_extract')
            
    if not args.dry_run and turns:
        # Checkpoint logic
        pass

if __name__ == "__main__":
    main()
