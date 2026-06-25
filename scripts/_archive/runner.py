#!/usr/bin/env python3
# Status: deprecated
# Path: migrated to — pipelines/exp_runner.py
"""DEPRECATED — replaced by exp_runner.py. Kept as compat shim for old experiment scripts."""
import sys, warnings
warnings.warn("runner.py is deprecated — use exp_runner.py instead", DeprecationWarning, stacklevel=2)
from pipelines.exp_runner import main as _deprecated_main

if __name__ == "__main__":
    sys.argv = [a.replace("runner.py", "exp_runner.py") for a in sys.argv]
    _deprecated_main()
