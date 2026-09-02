#!/usr/bin/env python3
"""Which lines of the engine the suite actually runs.

No dependencies: a line tracer is dropped onto PYTHONPATH so that every
subprocess the suite spawns - the CLI, the hooks, the watchers - records what it
executed, and the counts are added up afterwards. The engine runs almost
entirely in subprocesses, so tracing only this process would measure nothing.

    python3 tests/coverage.py            every suite
    python3 tests/coverage.py test_units.py test_robust.py

Lines inside a function that execs (the passthrough) are invisible to this: the
process image is replaced before anything can be written down.
"""

import ast
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "scripts", "agent_progress.py")

TRACER = '''
import atexit, os, sys, threading
TARGET, OUT = os.environ.get("APCOV_TARGET", ""), os.environ.get("APCOV_OUT", "")
if TARGET and OUT:
    _seen = set()

    def _trace(frame, event, arg):
        if event == "line" and frame.f_code.co_filename == TARGET:
            _seen.add(frame.f_lineno)
        return _trace

    def _dump():
        if _seen:
            try:
                with open(os.path.join(OUT, "cov-%d" % os.getpid()), "w") as f:
                    f.write("\\n".join(str(n) for n in sorted(_seen)))
            except OSError:
                pass

    atexit.register(_dump)
    threading.settrace(_trace)
    sys.settrace(_trace)
'''


def main():
    suites = sys.argv[1:] or ["all.py"]
    home = tempfile.mkdtemp(prefix="agent-progress-coverage-")
    out = os.path.join(home, "out")
    os.makedirs(out)
    with open(os.path.join(home, "usercustomize.py"), "w") as f:
        f.write(TRACER)
    env = dict(os.environ, PYTHONPATH=home, APCOV_TARGET=ENGINE, APCOV_OUT=out)
    for suite in suites:
        print("running %s ..." % suite)
        subprocess.run([sys.executable, os.path.join(ROOT, "tests", suite)],
                       cwd=os.path.join(ROOT, "tests"), env=env,
                       stdout=subprocess.DEVNULL)

    seen = set()
    for name in os.listdir(out):
        for line in open(os.path.join(out, name)):
            if line.strip().isdigit():
                seen.add(int(line.strip()))
    tree = ast.parse(open(ENGINE).read())
    executable = {n.lineno for n in ast.walk(tree)
                  if isinstance(n, ast.stmt)
                  and not isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    missed = executable - seen
    print()
    print("%d of %d statements reached (%.1f%%), %d never run"
          % (len(executable & seen), len(executable),
             100.0 * len(executable & seen) / max(1, len(executable)), len(missed)))

    funcs = []
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef):
            end = max([x.end_lineno for x in ast.walk(n) if hasattr(x, "end_lineno")]
                      or [n.lineno])
            funcs.append((n.lineno, end, n.name))
    funcs.sort()
    where = Counter()
    for m in missed:
        owner = "(module level)"
        for a, b, name in funcs:
            if a <= m <= b:
                owner = name
        where[owner] += 1
    print()
    print("least covered:")
    for name, count in where.most_common(15):
        print("  %-30s %3d unreached" % (name, count))
    shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
