#!/usr/bin/env python3
"""Run every test suite.

    python3 tests/all.py

Takes a couple of minutes: most of it is waiting on real processes, which is
the point - the threshold, the watcher and the crash path can only be checked
against a clock that is actually running.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = ["test_units.py", "test_lifecycle.py", "test_hooks.py",
          "test_agents.py", "test_robust.py", "test_properties.py", "test_slurm.py", "test_demo.py", "test_consistency.py", "test_mutations.py",
          "run_tests.py"]

total = failed = 0
broken = []
for name in SUITES:
    print("\n\033[1m%s\033[0m" % name)
    r = subprocess.run([sys.executable, os.path.join(HERE, name)])
    if r.returncode:
        broken.append(name)

print()
if broken:
    print("\033[38;5;203mfailing suites: %s\033[0m" % ", ".join(broken))
    sys.exit(1)
print("\033[38;5;42mall suites passed\033[0m")
