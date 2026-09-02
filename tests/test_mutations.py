#!/usr/bin/env python3
"""Does the suite actually catch anything?

A suite that passes tells you nothing on its own - it might be checking that
2 + 2 is 4. This breaks the engine on purpose, in ways that matter, and insists
the other suites notice. Each mutation is applied to a throwaway copy of the
plugin; the real one is never touched.

Every mutation here corresponds to a bug that was actually found and fixed in
this plugin, so what is being checked is that those bugs cannot come back
unnoticed.

    python3 tests/test_mutations.py
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile

import sandbox  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []
CHECKS = [0]


def ck(name, cond, detail=""):
    CHECKS[0] += 1
    print("  %s %s%s" % ("ok  " if cond else "FAIL", name, "" if cond else "   <- " + detail))
    if not cond:
        FAILS.append(name)


# (what is broken, the edit, which suite should notice, how long to allow)
MUTATIONS = [
    ("a finished job queues no report",
     '        elif job["state"] == "done" and load_config()["announce_done"]:',
     "        elif False:",
     "test_properties.py", 150),
    ("reports are handed to the wrong session",
     '        "session_id": job.get("session_id"),',
     '        "session_id": "somebody-else",',
     "test_properties.py", 150),
    ("the statusline stops filtering by session",
     '        if (scoped and cfg["scope"] == "session" and know_who_we_are(session_id)',
     '        if (False and cfg["scope"] == "session" and know_who_we_are(session_id)',
     "test_agents.py", 200),
    ("a not-a-number reaches the bar",
     "    return out if math.isfinite(out) else None",
     "    return out",
     "test_units.py", 120),
    ("the plugin forgets jobs that are still running",
     "    def removable(job):\n        return ours(job) and (args.force or not live(job))",
     "    def removable(job):\n        return ours(job)",
     "test_lifecycle.py", 200),
    ("nothing bounds a read from stdin",
     "            done = _complete_json(chunks)\n            if done is not None:\n                return done",
     "            pass",
     "test_robust.py", 260),
]


def mutated_copy(old, new):
    """A whole plugin, with one thing broken. Returns its directory, or None."""
    tmp = tempfile.mkdtemp(prefix="agent-progress-mutant-")
    for part in ("scripts", "hooks", "tests"):
        shutil.copytree(os.path.join(ROOT, part), os.path.join(tmp, part))
    engine = os.path.join(tmp, "scripts", "agent_progress.py")
    src = open(engine).read()
    if src.count(old) != 1:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    open(engine, "w").write(src.replace(old, new))
    return tmp


print("=== breaking the engine on purpose, to see if the suite notices ===")
for label, old, new, suite, budget in MUTATIONS:
    tmp = mutated_copy(old, new)
    if tmp is None:
        ck("%s: the mutation still applies" % label, False,
           "the code it targets has changed; update this mutation")
        continue
    env = dict(os.environ)
    env.pop("AGENT_PROGRESS_HOME", None)      # the copy gets its own sandbox
    env["APCHAOS_SEED"] = "5"
    # Output to a file, never a pipe. subprocess.run(timeout=) kills the child
    # and then waits for the pipes to close, and a grandchild - a watcher, or
    # the command a wrapper is waiting on - holds them open indefinitely, so the
    # timeout does not time anything out. This suite sat for thirty-five minutes
    # on exactly that.
    logfile = os.path.join(tmp, "suite.log")
    argv = [sys.executable, suite] + (["1"] if "properties" in suite else [])
    with open(logfile, "w") as out:
        proc = subprocess.Popen(argv, cwd=os.path.join(tmp, "tests"), stdout=out,
                                stderr=subprocess.STDOUT, env=env,
                                start_new_session=True)
        try:
            rc = proc.wait(timeout=budget)
            noticed = rc != 0
            why = ""
        except subprocess.TimeoutExpired:
            noticed, why = True, "the suite hung, which is also a failure"
    # the whole process group: a killed suite leaves watchers behind, and they
    # belong to this mutant rather than to anything the real plugin is doing
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        pass
    if not why:
        try:
            for line in open(logfile):
                if line.strip().startswith("FAIL"):
                    why = line.strip()[:66]
                    break
        except OSError:
            pass
    shutil.rmtree(tmp, ignore_errors=True)
    ck("%s is caught by %s" % (label, suite), noticed, "the suite passed anyway")
    if noticed and why:
        print("        %s" % why)

print()
print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)
