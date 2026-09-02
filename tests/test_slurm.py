#!/usr/bin/env python3
"""Slurm tracking, against a slurm that does what the test tells it to.

There is no cluster here, so `scontrol` and `sacct` are two scripts on PATH
that print whatever the test last wrote to a file. That is enough to be a real
end-to-end test: the engine shells out exactly as it would on a login node, and
the job moves through the queue because the fake scheduler says it did.

    python3 tests/test_slurm.py
"""

import importlib.util
import json
import os
import stat
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


# ------------------------------------------------------------- the fake cluster

BIN = os.path.join(sandbox.HOME, "fakebin")
os.makedirs(BIN, exist_ok=True)
SCONTROL_OUT = os.path.join(sandbox.HOME, "scontrol.txt")
SACCT_OUT = os.path.join(sandbox.HOME, "sacct.txt")

FAKE = """#!/bin/sh
[ -s "%s" ] || exit 1
cat "%s"
"""
for name, src in (("scontrol", SCONTROL_OUT), ("sacct", SACCT_OUT)):
    path = os.path.join(BIN, name)
    with open(path, "w") as f:
        f.write(FAKE % (src, src))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

os.environ["PATH"] = BIN + os.pathsep + os.environ.get("PATH", "")


def slurm_says(scontrol="", sacct=""):
    open(SCONTROL_OUT, "w").write(scontrol)
    open(SACCT_OUT, "w").write(sacct)


def run(*a, **kw):
    return subprocess.run([sys.executable, ENGINE] + list(a),
                          capture_output=True, text=True, **kw)


def jobs():
    return json.loads(run("ls", "--json").stdout or "[]")


def job(jid):
    for j in jobs():
        if j.get("id") == jid:
            return j
    return {}


def raw(jid):
    """The job record itself, for the fields `ls --json` does not publish."""
    return cc.state_ro()["jobs"].get(jid, {})


def until(predicate, seconds=150):
    """Wait for something a watcher has to notice.

    Generous on purpose. These loops stop the moment the predicate holds, so a
    high ceiling costs nothing when the machine is idle - and a ceiling tight
    enough to expire on a busy one turns a passing suite into a flaky one, which
    is worse than a slow test."""
    deadline = time.time() + seconds
    while time.time() < deadline and not predicate():
        time.sleep(1.0)
    return predicate()


PENDING = ("JobId=4242 JobName=eval UserId=me(1000) JobState=PENDING Reason=Resources "
           "Dependency=(null) Partition=gpu NumNodes=2 TimeLimit=02:00:00 "
           "RunTime=00:00:00 NodeList=(null) StdOut=%s/slurm-4242.out\n")
RUNNING = ("JobId=4242 JobName=eval UserId=me(1000) JobState=RUNNING Reason=None "
           "Dependency=(null) Partition=gpu NumNodes=2 TimeLimit=02:00:00 "
           "RunTime=00:07:30 NodeList=gpu-[3-4] StdOut=%s/slurm-4242.out\n")


print("=== reading slurm's own vocabulary ===")
ck("HH:MM:SS", cc.slurm_seconds("02:30:00") == 9000)
ck("DD-HH:MM:SS", cc.slurm_seconds("1-00:00:30") == 86430)
ck("MM:SS", cc.slurm_seconds("12:30") == 750)
ck("UNLIMITED is not a number", cc.slurm_seconds("UNLIMITED") is None)
ck("nor is nonsense", cc.slurm_seconds("later") is None)

ck("a plain array range", cc.count_tasks("1-9") == 9)
ck("a stepped range", cc.count_tasks("0-9:2") == 5)
ck("a list", cc.count_tasks("1,4,7") == 3)
ck("a concurrency cap is not a count", cc.count_tasks("1-100%4") == 100)
ck("a single task", cc.count_tasks("7") == 1)

ck("PENDING is queued", cc.classify_slurm("PENDING") == "queued")
ck("RUNNING is running", cc.classify_slurm("RUNNING") == "running")
ck("COMPLETED is done", cc.classify_slurm("COMPLETED") == "done")
ck("CANCELLED by someone is failed", cc.classify_slurm("CANCELLED by 1001") == "failed")
ck("OUT_OF_MEMORY is failed", cc.classify_slurm("OUT_OF_MEMORY") == "failed")
ck("a word slurm invents later is not an ending",
   cc.classify_slurm("SOMETHING_NEW") is None)

rec = cc.parse_scontrol(PENDING % sandbox.HOME)[0]
ck("scontrol: the state", rec["JobState"] == "PENDING", rec.get("JobState"))
ck("scontrol: the reason", rec["Reason"] == "Resources", rec.get("Reason"))
ck("scontrol: a (null) node list is still read", rec["NodeList"] == "(null)")
ck("scontrol: the output path",
   rec["StdOut"] == "%s/slurm-4242.out" % sandbox.HOME, rec.get("StdOut"))
ck("sacct: pipe-separated",
   cc.parse_sacct("COMPLETED|00:12:34|2026-09-01T10:00:00|0:0|gpu-1\n")[0]["Elapsed"]
   == "00:12:34")

print()
print("=== a job that is waiting says so, and says why ===")
slurm_says(scontrol=PENDING % sandbox.HOME)
r = run("slurm", "4242", "--name", "evalsuite", "--interval", "2s")
ck("attaching succeeds", r.returncode == 0, r.stderr[-200:])
j = job("evalsuite")
ck("it starts queued, not running", j.get("state") == "queued", str(j.get("state")))
ck("the reason is recorded", j.get("queue_reason") == "Resources", str(j.get("queue_reason")))
ck("so is the partition", j.get("partition") == "gpu", str(j.get("partition")))
ck("the log path comes from slurm, not a guess",
   j.get("log") == "%s/slurm-4242.out" % sandbox.HOME, str(j.get("log")))
ck("the time limit becomes the standing estimate",
   abs((raw("evalsuite").get("eta_prior_s") or 0) - 7200) < 1,
   str(raw("evalsuite").get("eta_prior_s")))

line = run("ls").stdout
ck("the bar says the word 'queued'", "queued" in line, repr(line[:200]))
ck("and translates slurm's reason", "waiting for nodes" in line, repr(line[:200]))
ck("no spinner on a job that is not turning",
   "⏳" in line, repr(line[:200]))

e = cc.estimate(job("evalsuite"))
ck("a queued job claims no fraction complete", e["frac"] is None, str(e["frac"]))
ck("and no remaining time", e["remaining"] is None, str(e["remaining"]))

print()
print("=== queue time is not run time ===")
time.sleep(1.2)
slurm_says(scontrol=RUNNING % sandbox.HOME)
open("%s/slurm-4242.out" % sandbox.HOME, "w").write("step 3/40\n")
ck("the watcher notices it started",
   until(lambda: job("evalsuite").get("state") == "running"),
   str(job("evalsuite").get("state")))
j, r = job("evalsuite"), raw("evalsuite")
ck("elapsed is slurm's RunTime, not time since submission",
   440 < (time.time() - (r.get("started") or 0)) < 470,
   "%.0fs" % (time.time() - (r.get("started") or 0)))
ck("and `ls` agrees", 440 < (j.get("elapsed_s") or 0) < 470, str(j.get("elapsed_s")))
ck("the queue wait is kept, separately",
   (r.get("queued_seconds") or 0) > 0, str(r.get("queued_seconds")))
ck("the nodes it landed on are recorded", j.get("nodes") == "gpu-[3-4]", str(j.get("nodes")))
ck("the reason is cleared once it is no longer waiting",
   not j.get("queue_reason"), str(j.get("queue_reason")))
ck("and the log is read once it has started",
   until(lambda: job("evalsuite").get("step") == 3), str(job("evalsuite").get("step")))

print()
print("=== the scheduler ends the job, and nothing else has to ===")
slurm_says(sacct="COMPLETED|00:20:00|2026-09-01T10:00:00|0:0|gpu-[3-4]\n")
until(lambda: job("evalsuite").get("state") != "running", 60)
ck("it finishes on the scheduler's word alone",
   job("evalsuite").get("state") == "done", str(job("evalsuite").get("state")))
run("rm", "--all", "--force")

print()
print("=== a failed job is a crash, reported like any other ===")
slurm_says(scontrol="JobId=77 JobState=PENDING Reason=Priority TimeLimit=01:00:00 "
                    "RunTime=00:00:00 NodeList=(null)\n")
run("slurm", "77", "--name", "doomed", "--interval", "2s")
ck("queued behind other work",
   "behind higher-priority jobs" in run("ls").stdout, run("ls").stdout[:200])
slurm_says(sacct="OUT_OF_MEMORY|00:04:10|2026-09-01T10:00:00|0:125|gpu-9\n")
until(lambda: job("doomed").get("state") not in ("queued", "running"), 60)
ck("it ends as failed", job("doomed").get("state") == "failed",
   str(job("doomed").get("state")))
ck("with slurm's own word kept",
   job("doomed").get("scheduler_state") == "OUT_OF_MEMORY",
   str(job("doomed").get("scheduler_state")))
# the listing, not a single drain: successful jobs queue a report too now, so
# whichever one drain happens to hand back is not the question being asked
inbox = run("inbox").stdout
ck("and a crash report waiting for Claude",
   "doomed" in inbox and "OUT_OF_MEMORY" in inbox, repr(inbox[:200]))
run("rm", "--all", "--force")

print()
print("=== an array is its own progress bar ===")
ARRAY = ("JobId=90_1 ArrayJobId=90 ArrayTaskId=1 JobState=RUNNING Reason=None "
         "TimeLimit=04:00:00 RunTime=00:10:00 NodeList=gpu-1\n"
         "JobId=90_[2-8] ArrayJobId=90 ArrayTaskId=2-8 JobState=PENDING "
         "Reason=Resources TimeLimit=04:00:00 RunTime=00:00:00 NodeList=(null)\n")
slurm_says(scontrol=ARRAY)
run("slurm", "90", "--name", "sweep", "--interval", "2s")
j = job("sweep")
ck("every task is counted", j.get("total") == 8, str(j.get("total")))
ck("a part-started array is running, not queued",
   j.get("state") == "running", str(j.get("state")))
ck("none finished yet", j.get("step") == 0, str(j.get("step")))

slurm_says(scontrol=("JobId=90_[7-8] ArrayJobId=90 ArrayTaskId=7-8 JobState=PENDING "
                     "Reason=Resources TimeLimit=04:00:00 RunTime=00:00:00 "
                     "NodeList=(null)\n"))
until(lambda: job("sweep").get("state") == "queued")
j = job("sweep")
ck("an array back in the queue reads as queued", j.get("state") == "queued",
   str(j.get("state")))
ck("the total does not shrink to what is left", j.get("total") == 8, str(j.get("total")))
run("rm", "--all", "--force")

print()
print("=== an unreachable cluster changes nothing ===")
slurm_says()                                   # both commands fail, printing nothing
run("slurm", "1234", "--name", "silent", "--interval", "2s")
j = job("silent")
ck("the job is still created", j.get("id") == "silent", str(j))
ck("and sits queued rather than being declared finished",
   j.get("state") == "queued", str(j.get("state")))
ck("with the default log path guessed",
   (j.get("log") or "").endswith("slurm-1234.out"), str(j.get("log")))
time.sleep(3)
ck("silence never ends a job", job("silent").get("state") == "queued",
   str(job("silent").get("state")))
run("rm", "--all", "--force")

print()
print("=== a queued job is never pruned, stalled or hidden ===")
slurm_says(scontrol=PENDING % sandbox.HOME)
run("slurm", "4242", "--name", "patient", "--interval", "2s")
with cc.state_rw() as st:
    st["jobs"]["patient"]["submitted"] = time.time() - 30 * 3600
    st["jobs"]["patient"]["started"] = time.time() - 30 * 3600
ck("a job queued for 30 hours is still there", job("patient").get("state") == "queued",
   str(job("patient")))
sl = subprocess.run([sys.executable, ENGINE, "statusline"], input="{}",
                    capture_output=True, text=True).stdout
ck("and is on the statusline", "patient" in sl, repr(sl[:200]))
ck("showing the wait, not a fake ETA", "queued 30h" in sl, repr(sl[:200]))
run("rm", "--all", "--force")

print()
print("=== a quiet job is not a stuck one while the scheduler says it runs ===")
slurm_says(scontrol="JobId=88 JobState=RUNNING Reason=None TimeLimit=3-00:00:00 "
                    "RunTime=1-02:00:00 NodeList=gpu-1\n")
run("slurm", "88", "--name", "quiet", "--interval", "2s")
sandbox.kill_watchers(cc, ["quiet"])
_w = subprocess.Popen([sys.executable, ENGINE, "_watch", "quiet", "--max-idle", "1",
                       "--interval", "2"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(7)
ck("a day of silence does not end a job the scheduler still reports running",
   job("quiet").get("state") == "running", "%s %r" % (job("quiet").get("state"), job("quiet").get("note")))
ck("and its watcher is still watching", _w.poll() is None)
slurm_says(sacct="COMPLETED|1-04:00:00|2026-09-01T10:00:00|0:0|gpu-1\n")
until(lambda: job("quiet").get("state") != "running", 60)
ck("so its real ending is still caught", job("quiet").get("state") == "done",
   str(job("quiet").get("state")))
try:
    _w.kill()
except OSError:
    pass
run("rm", "--all", "--force")

print()
print("=== with nobody to ask, silence is all there is ===")
_qlog = os.path.join(sandbox.HOME, "quiet2.log")
open(_qlog, "w").write("started\n")
run("start", "quiet2", "--log", _qlog, "--no-watch")
_w = subprocess.Popen([sys.executable, ENGINE, "_watch", "quiet2", "--max-idle", "1",
                       "--interval", "2"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ck("a bare log-attached job that goes quiet is still called stalled",
   until(lambda: job("quiet2").get("state") == "stalled", 30), str(job("quiet2").get("state")))
try:
    _w.kill()
except OSError:
    pass
run("rm", "--all", "--force")

print()
print("=== cancelling a scheduler job cancels it on the scheduler ===")
SCANCEL_LOG = os.path.join(sandbox.HOME, "scancel.txt")
SCANCEL_RC = os.path.join(sandbox.HOME, "scancel.rc")
_sc = os.path.join(BIN, "scancel")
with open(_sc, "w") as f:
    f.write('#!/bin/sh\necho "$@" >> "%s"\nexit $(cat "%s" 2>/dev/null || echo 0)\n'
            % (SCANCEL_LOG, SCANCEL_RC))
os.chmod(_sc, os.stat(_sc).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
slurm_says(scontrol="JobId=91 JobState=RUNNING Reason=None TimeLimit=01:00:00 "
                    "RunTime=00:10:00 NodeList=gpu-2\n")
run("slurm", "91", "--name", "stopme", "--interval", "2s")
r = run("cancel", "stopme")
ck("cancel succeeds", r.returncode == 0, r.stderr[-160:])
ck("and scancel was called with the job's id",
   os.path.exists(SCANCEL_LOG) and "91" in open(SCANCEL_LOG).read(),
   open(SCANCEL_LOG).read() if os.path.exists(SCANCEL_LOG) else "(never called)")
ck("the record says so", job("stopme").get("state") == "cancelled"
   and "scancel" in (job("stopme").get("note") or ""), str(job("stopme").get("note")))
ck("and the caller is told", "scancel" in r.stdout, r.stdout[-200:])
run("rm", "--all", "--force")

open(SCANCEL_RC, "w").write("1")
run("slurm", "91", "--name", "stubborn", "--interval", "2s")
r = run("cancel", "stubborn")
ck("a cancel the scheduler refuses fails", r.returncode != 0, str(r.returncode))
ck("and says the job is still running", "still queued or running" in r.stderr, r.stderr[-200:])
ck("and leaves the record alone", job("stubborn").get("state") in ("running", "queued"),
   str(job("stubborn").get("state")))
os.remove(SCANCEL_RC)
run("rm", "--all", "--force")

os.remove(_sc)
run("slurm", "91", "--name", "nocmd", "--interval", "2s")
_path = os.environ["PATH"]
os.environ["PATH"] = BIN            # nothing else on it, so no scancel anywhere
r = run("cancel", "nocmd")
os.environ["PATH"] = _path
ck("with no scancel to run, cancel fails rather than pretending",
   r.returncode != 0 and job("nocmd").get("state") in ("running", "queued"),
   "%s %s" % (r.returncode, r.stderr[-160:]))
run("rm", "--all", "--force")

print()
print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)
