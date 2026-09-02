#!/usr/bin/env python3
"""The plugin's promises, checked against sequences nobody wrote down.

Every other suite checks a case somebody thought of. This one builds random
situations - several sessions, jobs of random lengths that succeed or fail,
faults injected while they run - and afterwards insists that the things which
must always be true still are:

  1. a job that ends reaches a terminal state and stays there
  2. exactly one report is queued for it, of the right kind
  3. that report goes to the session that started the job, and to no other
  4. a report is delivered once, never twice
  5. the state file always parses, and every job in it is well formed
  6. a session's statusline shows its own jobs and no others
  7. nothing the plugin does kills a job: work that should finish, finishes

Failures print the seed. Re-run with APCHAOS_SEED=<n> to get the same sequence
back.

    python3 tests/test_properties.py [rounds]
"""

import importlib.util
import json
import os
import random
import re
import subprocess
import sys
import threading
import time

import sandbox  # noqa: F401  - must come first

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "scripts", "agent_progress.py")
HOOKS = os.path.join(ROOT, "hooks")
_spec = importlib.util.spec_from_file_location("agent_progress", ENGINE)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

FAILS = []
CHECKS = [0]
SEED = int(os.environ.get("APCHAOS_SEED") or random.randrange(1 << 30))
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 6
rng = random.Random(SEED)


def ck(name, cond, detail=""):
    CHECKS[0] += 1
    print("  %s %s%s" % ("ok  " if cond else "FAIL", name, "" if cond else "   <- " + detail))
    if not cond:
        FAILS.append(name)


def run(sid, *args):
    env = dict(os.environ)
    if sid:
        env["CLAUDE_CODE_SESSION_ID"] = sid
    else:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    return subprocess.run([sys.executable, ENGINE] + list(args),
                          capture_output=True, text=True, env=env)


def prompt(sid):
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID=sid)
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "inject_status.py"),
                          "UserPromptSubmit"], input=json.dumps({"session_id": sid}),
                         capture_output=True, text=True, env=env).stdout
    try:
        return json.loads(out or "{}").get("hookSpecificOutput", {}).get(
            "additionalContext", "")
    except ValueError:
        return ""


def statusline(sid):
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID=sid)
    out = subprocess.run([sys.executable, ENGINE, "statusline"],
                         input=json.dumps({"session_id": sid}),
                         capture_output=True, text=True, env=env)
    return out.returncode, re.sub(r"\033\[[0-9;]*m", "", out.stdout)


def state():
    with open(cc.STATE) as f:
        return json.load(f)


# ----------------------------------------------------------------- the faults

def fault_kill_watcher(jobs):
    st = state()
    live = [j for j in st["jobs"].values() if j.get("watcher_pid")]
    if live:
        victim = rng.choice(live)
        try:
            os.kill(int(victim["watcher_pid"]), 9)
        except OSError:
            pass


def fault_hold_lock(jobs):
    import fcntl

    def hold():
        try:
            lf = open(cc.LOCK, "a+")
            fcntl.flock(lf, fcntl.LOCK_EX)
            time.sleep(rng.uniform(0.2, 1.2))
            fcntl.flock(lf, fcntl.LOCK_UN)
            lf.close()
        except OSError:
            pass

    threading.Thread(target=hold, daemon=True).start()


def fault_stale_temp(jobs):
    p = os.path.join(sandbox.HOME, "state.json.tmp.%d" % rng.randrange(90000, 99999))
    try:
        open(p, "w").write("{}")
        os.utime(p, (time.time() - 4000,) * 2)
    except OSError:
        pass


def fault_junk_in_maps(jobs):
    try:
        with cc.state_rw(timeout=2.0) as st:
            st.setdefault("sessions", {})["junk-%d" % rng.randrange(999)] = "not a time"
            st.setdefault("auto_track_seen", {})["junk"] = {"nested": True}
            st.setdefault("context_sent", {})["junk"] = "not a dict"
    except Exception:
        pass


def fault_extra_prompt(jobs):
    prompt("sess-%d" % rng.randrange(4))


def fault_nothing(jobs):
    pass


FAULTS = [fault_kill_watcher, fault_hold_lock, fault_stale_temp,
          fault_junk_in_maps, fault_extra_prompt, fault_nothing, fault_nothing]


# ------------------------------------------------------------------ one round

def one_round(n):
    run(None, "rm", "--all", "--force", "--everywhere")
    with cc.state_rw() as st:
        st["inbox"] = []
    sessions = ["sess-%d" % i for i in range(rng.randint(1, 3))]
    jobs = []
    for i in range(rng.randint(2, 5)):
        sid = rng.choice(sessions)
        jid = "r%d-j%d" % (n, i)
        dies = rng.random() < 0.4
        secs = rng.uniform(0.5, 2.5)
        marker = os.path.join(sandbox.HOME, "done-" + jid)
        script = ("sleep %.2f; echo THE-OUTPUT-OF-%s; touch %s; exit %d"
                  % (secs, jid, marker, 3 if dies else 0))
        run(sid, "run", "--name", jid, "--eta", "1h", "--interval", "1s",
            "--", "sh", "-c", script)
        jobs.append({"id": jid, "session": sid, "dies": dies, "marker": marker})

    for _ in range(rng.randint(1, 4)):
        time.sleep(rng.uniform(0.2, 1.0))
        rng.choice(FAULTS)(jobs)

    # let them finish; prompts are what revive anything the faults broke
    deadline = time.time() + 120
    while time.time() < deadline:
        st = state()
        if all(st["jobs"].get(j["id"], {}).get("state") not in ("running", "queued", None)
               for j in jobs):
            break
        for sid in sessions:
            prompt(sid)
        time.sleep(1)
    return jobs, sessions


print("=== %d rounds of randomised jobs and faults (seed %d) ===" % (ROUNDS, SEED))
for round_no in range(ROUNDS):
    jobs, sessions = one_round(round_no)
    st = state()

    unfinished = [j["id"] for j in jobs
                  if st["jobs"].get(j["id"], {}).get("state") in ("running", "queued", None)]
    ck("round %d: every job reached an end" % round_no, not unfinished, str(unfinished))

    did_not_run = [j["id"] for j in jobs if not os.path.exists(j["marker"])]
    ck("round %d: and every job's work actually ran" % round_no,
       not did_not_run, str(did_not_run))

    wrong_state = [(j["id"], st["jobs"].get(j["id"], {}).get("state"))
                   for j in jobs
                   if st["jobs"].get(j["id"], {}).get("state")
                   != ("failed" if j["dies"] else "done")]
    ck("round %d: each ended the way its command did" % round_no,
       not wrong_state, str(wrong_state))

    reports = {}
    for e in st.get("inbox", []):
        reports.setdefault(e.get("job"), []).append(e)
    missing = [j["id"] for j in jobs if len(reports.get(j["id"], [])) != 1]
    ck("round %d: exactly one report each" % round_no, not missing,
       str({m: len(reports.get(m, [])) for m in missing}))

    miskind = [j["id"] for j in jobs
               if reports.get(j["id"]) and reports[j["id"]][0].get("kind")
               != ("crash" if j["dies"] else "done")]
    ck("round %d: of the right kind" % round_no, not miskind, str(miskind))

    misowned = [j["id"] for j in jobs
                if reports.get(j["id"])
                and reports[j["id"]][0].get("session_id") != j["session"]]
    ck("round %d: owned by the session that started it" % round_no,
       not misowned, str(misowned))

    twice = [e.get("job") for e in st.get("inbox", [])
             if isinstance(e.get("delivered"), dict) and e.get("delivered_count", 1) > 1]
    ck("round %d: nothing delivered twice" % round_no, not twice, str(twice))

    bad_state = [k for k, v in st["jobs"].items() if not isinstance(v, dict) or "state" not in v]
    ck("round %d: every record is well formed" % round_no, not bad_state, str(bad_state))

    leaks, crashes = [], []
    for sid in sessions:
        rc, text = statusline(sid)
        if rc != 0:
            crashes.append((sid, rc))
        for j in jobs:
            if j["session"] != sid and j["id"] in text:
                leaks.append((sid, j["id"]))
    # duplicate watchers have been a bug twice: two sessions reviving the same
    # job at once, and a revival racing the watcher it was replacing
    doubled = []
    for j in jobs:
        out = subprocess.run(["pgrep", "-f", "_watch " + j["id"]],
                             capture_output=True, text=True).stdout.split()
        if len(out) > 1:
            doubled.append((j["id"], len(out)))
    ck("round %d: no job ended up with two watchers" % round_no, not doubled, str(doubled))

    ck("round %d: no statusline crashed" % round_no, not crashes, str(crashes))
    ck("round %d: and none showed another session's job" % round_no, not leaks, str(leaks))

run(None, "rm", "--all", "--force", "--everywhere")
sandbox.kill_watchers(cc)
print()
print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
if FAILS:
    print("   reproduce with: APCHAOS_SEED=%d python3 tests/test_properties.py" % SEED)
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)
