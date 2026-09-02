#!/usr/bin/env python3
"""The demo recording's panels, without the rendering.

`demo/record.py` can only produce its .mov on a Mac - it wants Menlo and
swiftc. What it does *before* that is ordinary and worth checking anywhere:
each panel sends its command through the real PreToolUse hook, runs whatever
comes back, and finds the job the engine created. This drives exactly that,
with the recording's own stub scheduler on a compressed clock.

    python3 tests/test_demo.py
"""

import importlib.util
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "scripts", "agent_progress.py")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sandbox  # noqa: E402

spec = importlib.util.spec_from_file_location("agent_progress", ENGINE)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

FAILS = []
CHECKS = [0]


def ck(name, cond, detail=""):
    CHECKS[0] += 1
    print("  %s %s%s" % ("ok  " if cond else "FAIL", name, "" if cond else "   <- " + detail))
    if not cond:
        FAILS.append(name)


try:
    from PIL import Image  # noqa: F401
except ImportError:
    print("  skipped: demo/record.py needs Pillow")
    sys.exit(0)

_rspec = importlib.util.spec_from_file_location(
    "record", os.path.join(ROOT, "demo", "record.py"))
rec = importlib.util.module_from_spec(_rspec)
_rspec.loader.exec_module(rec)

print("=== the columns, left to right ===")
keys = [p["key"] for p in rec.PANELS]
ck("three panels", len(keys) == 3, str(keys))
ck("the build is the rightmost column", keys[-1] == "make", str(keys))
ck("and no longer the leftmost", keys[0] != "make", str(keys))
ck("the two jobs with bars sit together",
   keys[:2] == ["train", "benchmark"], str(keys))

bench = dict((p["key"], p) for p in rec.PANELS)["benchmark"]
ck("the benchmark is submitted, not run here",
   bench["command"].startswith("sbatch"), bench["command"])
ck("it is tracked under the id slurm gives it",
   bench["jid"] == "slurm-81734", str(bench.get("jid")))
ck("the build panel expects no job at all",
   dict((p["key"], p) for p in rec.PANELS)["make"]["jid"] is None)

print()
print("=== the hook rewrites each panel's command ===")
scratch = os.path.join(sandbox.HOME, "rec")
os.makedirs(os.path.join(scratch, "bin"), exist_ok=True)
for p in rec.PANELS:
    open(os.path.join(scratch, p["file"]), "w").write(p["body"])

# long enough on each side of the transition that a watcher waking on its
# 15-second cap still lands inside the running window
QUEUED, RAN = 6, 25
for name, body in rec.SLURM_STUBS.items():
    path = os.path.join(scratch, "bin", name)
    open(path, "w").write(rec.stub_text(body, QUEUED, RAN))
    os.chmod(path, 0o755)
os.environ["PATH"] = os.path.join(scratch, "bin") + os.pathsep + os.environ["PATH"]
os.environ["AP_FAKE_SLURM"] = scratch

panels = [rec.Panel(cc, p, scratch) for p in rec.PANELS]
by_key = dict((p.spec["key"], p) for p in panels)
for pan in panels:
    ck("%s: the hook is willing to track it" % pan.spec["key"],
       cc.classify_command(pan.spec["command"], {}, cc.load_config())["track"],
       pan.spec["command"])

print()
print("=== the benchmark goes through the queue, for real ===")
submitted_at = time.time()
by_key["benchmark"].launch()
# Generous ceilings. The demo runs on a wall clock - its stub scheduler queues
# for a fixed number of seconds - so on a machine running other suites at the
# same time the whole thing slips. Each loop stops the moment its condition
# holds, so waiting longer costs nothing when the machine is idle.
deadline = time.time() + 240
while time.time() < deadline and by_key["benchmark"].job() is None:
    time.sleep(0.5)
j = by_key["benchmark"].job() or {}
ck("submitting produces a tracked job", bool(j), "no job appeared")
ck("named for the queue and the id slurm printed", j.get("id") == "slurm-81734",
   str(j.get("id")))
ck("which starts queued", j.get("state") == "queued", str(j.get("state")))
ck("with slurm's reason for the wait", j.get("queue_reason") == "Resources",
   str(j.get("queue_reason")))

cfg = dict(cc.load_config())
cfg.update(bar_width=10, show_name=False, show_rate=False, show_eta_clock=False,
           show_drift=False, show_note=False, show_counts=False, name_width=10)
line = cc.render_line(j, cfg, width=rec.PANEL_COLS)
ck("the bar in a 42-column panel says queued", "queued" in line, repr(line))
# the column is 42 wide, so the tail of the phrase is elided; the half
# that matters is the half that survives
ck("and why", "waiting for" in line, repr(line))
ck("and fits the column", cc.visible_len(line) <= rec.PANEL_COLS,
   "%d columns" % cc.visible_len(line))

os.system("%s %s update slurm-81734 --interval 1s --quiet >/dev/null 2>&1"
          % (sys.executable, ENGINE))
deadline = time.time() + 300
while time.time() < deadline and (by_key["benchmark"].job() or {}).get("state") == "queued":
    time.sleep(0.5)
j = by_key["benchmark"].job() or {}
ck("the queue lets it start", j.get("state") == "running", str(j.get("state")))
elapsed = time.time() - (j.get("started") or 0)
since_submit = time.time() - submitted_at
ck("and the clock is slurm's runtime, not time since submission",
   elapsed < since_submit - QUEUED + 3 and elapsed >= 0,
   "%.0fs of run time out of %.0fs since submitting, %ds of it queued"
   % (elapsed, since_submit, QUEUED))

# Two separate things, and conflating them made this fail under load: first the
# stub scheduler has to reach the end of its own wall-clock script, then the
# plugin has to notice. Wait for the stub on its own terms - ask it, as the
# plugin does - and only then hold the plugin to a deadline.
def scheduler_says():
    out = subprocess.run(["sacct", "-j", "81734", "-n", "-o", "State"],
                         capture_output=True, text=True).stdout
    if not out.strip():
        out = subprocess.run(["scontrol", "show", "job", "81734"],
                             capture_output=True, text=True).stdout
    return out


stub_deadline = time.time() + 600
while time.time() < stub_deadline and "OUT_OF_MEMORY" not in scheduler_says():
    time.sleep(0.5)
ck("the stub scheduler reached the end of its script",
   "OUT_OF_MEMORY" in scheduler_says(), scheduler_says()[:60])

deadline = time.time() + 180
while time.time() < deadline and (by_key["benchmark"].job() or {}).get("state") == "running":
    time.sleep(0.5)
j = by_key["benchmark"].job() or {}
ck("slurm ends it, and says how", j.get("state") == "failed", str(j.get("state")))
ck("keeping the word it used", j.get("scheduler_state") == "OUT_OF_MEMORY",
   str(j.get("scheduler_state")))

by_key["benchmark"].drain()
banner = [l for l in by_key["benchmark"].lines if "agent-progress" in l]
ck("the panel shows a submission banner, not the handoff one",
   any("queued" in l for l in banner), str(banner))

os.system("%s %s rm --all >/dev/null 2>&1" % (sys.executable, ENGINE))

print()
print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)
