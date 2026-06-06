#!/usr/bin/env python3
# Status: production
# Path: systemd timer
"""Watchdog for J model comparison tests.
Checks progress periodically and sends Slack updates.
Called by systemd timer / cron every 10 minutes.

Usage: python3 watchdog_j_tests.py [--final]

State file: /opt/projects/server/data/experiment/.j_test_watchdog.json
"""
import json, os, sys, time
sys.path.insert(0, '/opt/projects/server/scripts')
from pipelines.code_mod import _notify_slack

EXPER_DIR = '/opt/projects/server/data/experiment'
OUTPUT_FILE = '/var/tmp/claude-1000/-home-opc/c460d682-5ac4-4bcb-a40e-3716ed1882b8/tasks/bebzb6139.output'
STATE_FILE = f'{EXPER_DIR}/.j_test_watchdog.json'
BASELINE = f'{EXPER_DIR}/exp_j_baseline.json'
COMPARISON = f'{EXPER_DIR}/j_model_comparison.json'

MODELS = ['Qwen14B', 'DeepCoder', 'NextCoder', 'IQuest']

def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {'last_line_count': 0, 'notified_models': [], 'completed': False}

def save_state(s):
    with open(STATE_FILE, 'w') as f:
        json.dump(s, f)

def read_output():
    if not os.path.exists(OUTPUT_FILE):
        return ''
    with open(OUTPUT_FILE) as f:
        return f.read()

def check_progress(output):
    result = {}
    for m in MODELS:
        if f'Testing {m}' in output:
            result[m] = 'testing'
        if f'{m} (normal):' in output and m not in result:
            result[m] = 'completed'
    return result

def send_update(msg):
    _notify_slack(f'🤖 [Watchdog] J Model Test\n{msg}')

def main(final=False):
    state = load_state()
    output = read_output()
    lines = output.count('\n')

    # Check completion
    if os.path.exists(COMPARISON):
        data = json.load(open(COMPARISON))
        state['completed'] = True
        save_state(state)

        # Build summary
        lines_out = []
        for r in data.get('results', []):
            m = r.get('model','?')
            lbl = r.get('label','?')
            res = r.get('result', {}) or {}
            p = res.get('P_score', '?')
            r_score = res.get('R_score', '?')
            c = res.get('consensus_score', '?')
            d = res.get('decision', '?')
            lines_out.append(f'{m:12} {lbl:8} P={p} R={r_score} Consensus={c} {d}')

        report = '✅ **All Tests Complete**\n\n'
        report += '\n'.join(lines_out)
        report += f'\n\nSaved: j_model_comparison.json'
        _notify_slack(report)
        print('Final report sent')
        return

    # Check progress
    progress = check_progress(output)
    new_models = [m for m in MODELS if m in progress and m not in state.get('notified_models', [])]

    if new_models:
        lines_out = []
        for m in MODELS:
            s = progress.get(m, 'waiting')
            if s == 'completed' or m == 'Qwen14B':
                # Get result
                if os.path.exists(BASELINE) and m == 'Qwen14B':
                    d = json.load(open(BASELINE))
                    for r in d.get('Qwen14B', []):
                        res = r.get('result', {})
                        lbl = r.get('label', '?')
                        lines_out.append(f'{m:12} {lbl:8} P={res.get("P_score","?")} R={res.get("R_score","?")} C={res.get("consensus_score","?")} ✅')
                elif m in progress and m in state.get('notified_models', []):
                    lines_out.append(f'{m:12} ✅ done')
            elif s == 'testing':
                lines_out.append(f'{m:12} ▶ inference 중')
            else:
                lines_out.append(f'{m:12} ⏳ 대기중')

        msg = '\n'.join(lines_out)
        state['notified_models'] = state.get('notified_models', []) + new_models
        send_update(msg)

    state['last_line_count'] = lines
    save_state(state)

if __name__ == '__main__':
    main('--final' in sys.argv)
