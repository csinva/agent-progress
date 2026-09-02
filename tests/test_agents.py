#!/usr/bin/env python3
"""Several Claude sessions at once.

`claude agents` runs many sessions on one machine, and they all share one state
file. A bar belongs to the session whose work it is: an agent should see its own
jobs and nobody else's, and a crash is news for the session that started the job
- telling another agent about it is both wrong and, because the report is then
marked delivered, the reason the right agent never hears.

Everything here drives the real CLI and the real hooks with a session id in the
environment, which is what a session actually gives them.

    python3 tests/test_agents.py
"""

import importlib.util
import json
import os
import re
import shutil
import signal
import tempfile
import subprocess
import sys
import threading
import time

import sandbox  # noqa: F401  - must come first; points the engine at a temp dir

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "scripts", "agent_progress.py")
HOOKS = os.path.join(ROOT, "hooks")
INJECT = os.path.join(HOOKS, "inject_status.py")
_spec = importlib.util.spec_from_file_location("agent_progress", ENGINE)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

FAILS = []
CHECKS = [0]


def ck(name, cond, detail=""):
    CHECKS[0] += 1
    print("  %s %s%s" % ("ok  " if cond else "FAIL", name, "" if cond else "   <- " + detail))
    if not cond:
        FAILS.append(name)


def as_session(sid, *args, **kw):
    """Run the CLI the way a session would: its id in the environment."""
    env = dict(os.environ)
    if sid:
        env["CLAUDE_CODE_SESSION_ID"] = sid
    else:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    return subprocess.run([sys.executable, ENGINE] + list(args),
                          capture_output=True, text=True, env=env, **kw)


LAST_DRAW = {}


def bars(sid):
    """The job names on that session's statusline.

    Records why a draw came back empty. This check failed once on a machine
    running two whole suites at the same time and could not be reproduced in
    288 tries afterwards; a bare list of names tells you nothing about which of
    the many possible reasons it was."""
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID=sid)
    proc = subprocess.run([sys.executable, ENGINE, "statusline"],
                          input=json.dumps({"session_id": sid}),
                          capture_output=True, text=True, env=env)
    out = proc.stdout
    LAST_DRAW[sid] = "exit=%d stdout=%r stderr=%r" % (
        proc.returncode, out[:120], proc.stderr[-160:])
    names = []
    for line in out.splitlines():
        plain = re.sub(r"\033\[[0-9;]*m", "", line).strip()
        if not plain or plain.startswith(("⏺", "+")):
            continue
        parts = plain.split()
        if len(parts) > 1:
            names.append(parts[1])
    return sorted(names)


def context(sid):
    """What this session is told about jobs that ended, on whichever channel.

    By default that is the side channel - a systemMessage shown beside the
    conversation when a turn ends - and nothing rides along with the user's
    messages at all. The routing being checked is the same either way: news
    about a job goes to the session whose job it is."""
    out = subprocess.run([sys.executable, INJECT, "Stop"],
                         input=json.dumps({"session_id": sid, "stop_hook_active": False}),
                         capture_output=True, text=True,
                         env=dict(os.environ, CLAUDE_CODE_SESSION_ID=sid)).stdout
    try:
        got = json.loads(out or "{}")
    except ValueError:
        return ""
    return got.get("systemMessage") or got.get("reason") or ""


def named(text, candidates):
    """Which of these job ids the report mentions.

    Written this way so the check survives the report being reworded: the
    question is which jobs were named, not how they were quoted."""
    return sorted(c for c in candidates if re.search(r"\b%s\b" % re.escape(c), text or ""))


def in_context(sid):
    """What rides along with the user's message - nothing, unless asked for."""
    out = subprocess.run([sys.executable, INJECT, "UserPromptSubmit"],
                         input=json.dumps({"session_id": sid}),
                         capture_output=True, text=True,
                         env=dict(os.environ, CLAUDE_CODE_SESSION_ID=sid)).stdout
    try:
        return json.loads(out or "{}").get("hookSpecificOutput", {}).get(
            "additionalContext", "")
    except ValueError:
        return ""


def reset():
    as_session(None, "rm", "--all", "--force")
    as_session(None, "config", "--reset")
    with cc.state_rw() as st:
        st["inbox"] = []
        st["context_sent"] = {}


def queue_crash(job, sid, ts=None):
    with cc.state_rw() as st:
        st.setdefault("inbox", []).append(
            {"job": job, "ts": ts or time.time(), "exit_code": 1, "duration": 5,
             "reason": "exited with status 1", "reason_short": "exit 1",
             "session_id": sid, "delivered": None})


print("=== each agent sees its own work and no one else's ===")
for trial in range(6):
    reset()
    agents = ["agent-%d" % i for i in range(2 + trial)]
    for a in agents:
        as_session(a, "start", "job-" + a, "--eta", "3h", "--monitor", "time", "--no-watch")
    leaked = {a: [n for n in bars(a) if n != "job-" + a] for a in agents}
    missing = [a for a in agents if "job-" + a not in bars(a)]
    ck("%d agents: each sees only its own" % len(agents),
       not any(leaked.values()) and not missing,
       "leaked=%s missing=%s" % ({k: v for k, v in leaked.items() if v}, missing))

print()
print("=== a job nobody owns is shown to everyone ===")
reset()
as_session(None, "start", "orphan", "--eta", "3h", "--monitor", "time", "--no-watch")
as_session("agent-A", "start", "mine-A", "--eta", "3h", "--monitor", "time", "--no-watch")
ck("the unowned job reaches agent A", "orphan" in bars("agent-A"), str(bars("agent-A")))
ck("and agent B, which owns nothing", bars("agent-B") == ["orphan"], str(bars("agent-B")))
ck("agent B is not shown agent A's job", "mine-A" not in bars("agent-B"), str(bars("agent-B")))

print()
print("=== what each agent is told ===")
reset()
for a in ("agent-A", "agent-B"):
    as_session(a, "start", "job-" + a, "--eta", "3h", "--monitor", "time", "--no-watch")
# once each: an unchanged summary is deliberately not resent, so asking twice
# in one expression answers the second time with nothing
# Running jobs are shown on the statusline, which is the side channel for
# them; nothing about them is put into the model's context any more.
ck("agent A sees its own job on its bar", bars("agent-A") == ["job-agent-A"],
   str(bars("agent-A")))
ck("agent B sees only its own", bars("agent-B") == ["job-agent-B"], str(bars("agent-B")))
ck("and neither one's prompt carries anything to the model",
   in_context("agent-A") == "" and in_context("agent-B") == "",
   repr(in_context("agent-A")[:60]))

print()
print("=== a crash goes to the session whose job it was ===")
for trial in range(6):
    reset()
    agents = ["agent-%d" % i for i in range(4)]
    for a in agents:
        queue_crash("job-" + a, a)
    got = {}
    for a in agents:
        text = context(a)
        got[a] = named(text, ["job-" + x for x in agents])
    right = all(got[a] == ["job-" + a] for a in agents)
    ck("trial %d: four crashes, four owners" % (trial + 1), right, str(got))

print()
print("=== nothing is lost when the owner has gone ===")
reset()
grace = cc.orphan_grace()
queue_crash("abandoned", "an-agent-that-exited", ts=time.time() - grace - 60)
handed = context("someone-else")
ck("a stale crash is eventually offered to whoever is here", "abandoned" in handed, handed[:90])
ck("and it is labelled as another session's",
   "another session" in handed.lower(), handed[:120])
reset()
queue_crash("fresh", "an-agent-still-running")
ck("but a fresh one is not taken from its owner",
   "fresh" not in context("someone-else"), context("someone-else")[:90])
ck("and its owner still gets it", "fresh" in context("an-agent-still-running"))

ck("an hour, not ten minutes, is the wait", grace >= 3600, str(grace))
reset()
queue_crash("still-owned", "a-live-but-idle-agent", ts=time.time() - 900)
ck("a report 15 minutes old is still its owner's",
   "still-owned" not in context("a-passing-agent"), context("a-passing-agent")[:80])
ck("and the owner still receives it, unlabelled",
   "still-owned" in context("a-live-but-idle-agent")
   and "another session" not in context("a-live-but-idle-agent").lower())

print()
print("=== many agents at once ===")
for trial in range(4):
    reset()
    agents = ["agent-%02d" % i for i in range(12)]
    for a in agents:
        as_session(a, "start", "job-" + a, "--eta", "3h", "--monitor", "time", "--no-watch")
        queue_crash("crash-" + a, a)
    seen, lock = {}, threading.Lock()

    def look(a):
        rows, text = bars(a), context(a)
        with lock:
            seen[a] = (rows, named(text, ["crash-" + x for x in agents]))

    th = [threading.Thread(target=look, args=(a,)) for a in agents]
    [t.start() for t in th]
    [t.join() for t in th]
    bad_bars = {a: v[0] for a, v in seen.items() if v[0] != ["job-" + a]}
    bad_crash = {a: v[1] for a, v in seen.items() if v[1] != ["crash-" + a]}
    ck("trial %d: 12 agents concurrently, bars stay put" % (trial + 1),
       not bad_bars,
       "%s | last draw: %s" % (dict(list(bad_bars.items())[:2]),
                               "; ".join(LAST_DRAW.get(a, "?") for a in list(bad_bars)[:1])))
    ck("trial %d: 12 agents concurrently, crashes stay put" % (trial + 1),
       not bad_crash, str(dict(list(bad_crash.items())[:3])))

print()
print("=== a job started through the hook belongs to the session that ran it ===")
reset()
AUTO = os.path.join(HOOKS, "auto_track.py")
procs = []
for sid in ("agent-X", "agent-Y"):
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID=sid)
    out = subprocess.run([sys.executable, AUTO], env=env, capture_output=True, text=True,
                         input=json.dumps({"tool_name": "Bash", "session_id": sid,
                                           "tool_input": {"command": "python3 train.py"}})).stdout
    wrapped = json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"]
    wrapped = wrapped.replace("--after 20", "--after 1").replace(
        "--name train", "--name train-" + sid)
    # The wrapper waits for its command now, so the bar has to be looked at
    # while the command is still running rather than after the call returns.
    procs.append(subprocess.Popen(
        ["/bin/sh", "-c", wrapped.replace("python3 train.py", "sleep 12")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env))

_deadline = time.time() + 30
while time.time() < _deadline:
    raw = json.loads(open(sandbox.STATE).read())["jobs"]
    if {"train-agent-X", "train-agent-Y"} <= set(raw):
        break
    time.sleep(0.5)
raw = json.loads(open(sandbox.STATE).read())["jobs"]
owners = {k: v.get("session_id") for k, v in raw.items()}
ck("the wrapped job records the session that ran it",
   owners.get("train-agent-X") == "agent-X" and owners.get("train-agent-Y") == "agent-Y",
   str(owners))
for sid in ("agent-X", "agent-Y"):        # long enough to earn a bar
    as_session(sid, "update", "train-" + sid, "--eta", "3h", "--quiet")
ck("and each agent sees only its own", bars("agent-X") == ["train-agent-X"], str(bars("agent-X")))
for _p in procs:
    _p.kill()
    _p.wait()
for sid in ("agent-X", "agent-Y"):
    as_session(sid, "cancel", "train-" + sid)

print()
print("=== tidying up in one agent leaves the others alone ===")
reset()
for a in ("agent-A", "agent-B"):
    as_session(a, "start", "job-" + a, "--eta", "3h", "--monitor", "time", "--no-watch")
as_session("agent-A", "rm", "--all")
left = sorted(json.loads(open(sandbox.STATE).read())["jobs"])
ck("agent A's rm --all took only agent A's job", left == ["job-agent-B"], str(left))
as_session("agent-A", "rm", "--all", "--everywhere", "--force")
ck("--everywhere still clears the lot",
   not json.loads(open(sandbox.STATE).read())["jobs"])
as_session("agent-A", "start", "solo", "--eta", "3h", "--monitor", "time", "--no-watch")
as_session(None, "rm", "--all", "--force")
ck("and from a plain shell it means everything",
   not json.loads(open(sandbox.STATE).read())["jobs"])

reset()
for a in ("agent-A", "agent-B"):
    as_session(a, "start", "done-" + a, "--eta", "3h", "--monitor", "time", "--no-watch")
    with cc.state_rw() as st:
        st["jobs"]["done-" + a]["started"] = time.time() - 600
    as_session(a, "done", "done-" + a)
as_session("agent-A", "rm", "--finished")
left = sorted(json.loads(open(sandbox.STATE).read())["jobs"])
ck("rm --finished is scoped the same way", left == ["done-agent-B"], str(left))
out = as_session("agent-A", "rm", "--finished").stdout
ck("and says what it left for another session", "other sessions" in out, out.strip()[:80])
out = as_session("agent-A", "rm", "--all").stdout
ck("rm --all says so too", "other sessions" in out, out.strip()[:80])
ck("agent B can still see its finished job", bars("agent-B") == ["done-agent-B"],
   str(bars("agent-B")))

print()
print("=== one agent cannot act on another's job by name ===")
reset()
as_session("agent-A", "start", "train", "--eta", "3h", "--monitor", "time", "--no-watch")
as_session("agent-B", "start", "train", "--eta", "3h", "--monitor", "time", "--no-watch")
raw = json.loads(open(sandbox.STATE).read())["jobs"]
ck("the second agent's job gets its own id", sorted(raw) == ["train", "train-2"], str(sorted(raw)))
r = as_session("agent-B", "cancel", "train")
ck("cancelling across sessions is refused",
   r.returncode != 0 and "another session" in (r.stderr + r.stdout), (r.stderr or r.stdout)[:80])
ck("and the other agent's job is untouched",
   json.loads(open(sandbox.STATE).read())["jobs"]["train"]["state"] == "running")
ck("its own job it can cancel", as_session("agent-B", "cancel", "train-2").returncode == 0)
ck("reading another session's job is allowed",
   as_session("agent-B", "show", "train").returncode == 0)
for verb in ("done", "fail", "update"):
    args = ["update", "train", "--eta", "1h", "--quiet"] if verb == "update" else [verb, "train"]
    ck("%s is refused across sessions too" % verb,
       as_session("agent-B", *args).returncode != 0)
ck("rm by name is refused too", as_session("agent-B", "rm", "train").returncode != 0)
ck("--any-session is the way through",
   as_session("agent-B", "cancel", "train", "--any-session").returncode == 0)
reset()
as_session("agent-A", "start", "solo", "--eta", "3h", "--monitor", "time", "--no-watch")
ck("and a plain shell is never restricted",
   as_session(None, "cancel", "solo").returncode == 0)

print()
print("=== a noisy agent cannot bury a quiet one's crash ===")
reset()
queue_crash("quiet-agents-job", "agent-quiet")
for i in range(60):
    with cc.state_rw() as st:
        cc.enqueue_crash(st, {"id": "noisy-%d" % i, "exit_code": 1, "started": 1,
                              "ended": 2, "session_id": "agent-noisy"}, time.time())
inbox = json.loads(open(sandbox.STATE).read())["inbox"]
ck("the quiet agent's report is still queued",
   any(e.get("job") == "quiet-agents-job" for e in inbox), "%d entries" % len(inbox))
ck("and it is still the quiet agent that gets it",
   "quiet-agents-job" in context("agent-quiet"))
with cc.state_rw() as st:
    for e in st["inbox"][:-5]:
        e["delivered"] = {"session_id": "x", "ts": time.time()}
    before = sum(1 for e in st["inbox"] if not e.get("delivered"))
for i in range(30):
    with cc.state_rw() as st:
        cc.enqueue_crash(st, {"id": "more-%d" % i, "exit_code": 1, "started": 1,
                              "ended": 2, "session_id": "agent-noisy"}, time.time())
inbox = json.loads(open(sandbox.STATE).read())["inbox"]
ck("reports already handed over are the ones dropped",
   sum(1 for e in inbox if not e.get("delivered")) >= before,
   "%d undelivered" % sum(1 for e in inbox if not e.get("delivered")))
for i in range(300):
    with cc.state_rw() as st:
        cc.enqueue_crash(st, {"id": "flood-%d" % i, "exit_code": 1, "started": 1,
                              "ended": 2, "session_id": "agent-flood"}, time.time())
inbox = json.loads(open(sandbox.STATE).read())["inbox"]
ck("but the queue is still bounded", len(inbox) <= cc.CRASH_CEILING, "%d entries" % len(inbox))
reset()

print()
print("=== a bar must not vanish because nobody said which session this is ===")
# Scoping by session is only meaningful when the session is known. Neither the
# statusline payload nor the environment is guaranteed to carry the id, and
# filtering on an unknown identity matches nothing - so every bar disappeared
# while its job was still running, which is the one failure the plugin exists
# to prevent.
reset()
as_session("sess-A", "start", "train", "--eta", "2h", "--monitor", "time", "--no-watch")


def draw(payload, with_env):
    e = dict(os.environ)
    if with_env:
        e["CLAUDE_CODE_SESSION_ID"] = "sess-A"
    else:
        e.pop("CLAUDE_CODE_SESSION_ID", None)
    out = subprocess.run([sys.executable, ENGINE, "statusline"], input=json.dumps(payload),
                         capture_output=True, text=True, env=e).stdout
    return "train" in re.sub(r"\033\[[0-9;]*m", "", out)


ck("payload and environment both know it", draw({"session_id": "sess-A"}, True))
ck("only the payload knows it", draw({"session_id": "sess-A"}, False))
ck("only the environment knows it", draw({}, True))
ck("neither knows it - the bar is still drawn", draw({}, False))
reset()

print()
print("=== both kinds of job, from several agents, at once ===")
# The two kinds behave differently on purpose now: a command the caller waits
# for hands back its own output and is not reported, while a detached one is
# reported to the session that started it. Running both together is where a
# rule about one could quietly be applied to the other.
reset()
_work = tempfile.mkdtemp(prefix="agent-progress-mixed-")
open(os.path.join(_work, "t.py"), "w").write(
    "import sys, time\n"
    "die = sys.argv[1] == 'die'\n"
    "for i in range(1, 5):\n"
    "    print('Epoch %d/4' % i, flush=True); time.sleep(0.3)\n"
    "if die: raise SystemExit(7)\n")
_waited, _detached, _lock = {}, {}, threading.Lock()


def _wait_for(a):
    sid, name, die = "mix-%d" % a, "waited-%d" % a, a % 2 == 0
    r = as_session(sid, "exec", "--name", name, "--after", "1", "--shell",
                   "%s %s/t.py %s" % (sys.executable, _work, "die" if die else "live"))
    with _lock:
        _waited[name] = (sid, die, r.returncode, "Epoch 4/4" in r.stdout)


def _detach(a):
    sid, name, die = "mix-%d" % a, "detached-%d" % a, a % 2 == 1
    as_session(sid, "run", "--name", name, "--eta", "1h", "--",
               sys.executable, os.path.join(_work, "t.py"), "die" if die else "live")
    with _lock:
        _detached[name] = sid


_th = [threading.Thread(target=_wait_for, args=(a,)) for a in range(4)]
_th += [threading.Thread(target=_detach, args=(a,)) for a in range(4)]
[t.start() for t in _th]
[t.join() for t in _th]
ck("every caller that waited got its own exit code",
   all(rc == (7 if die else 0) for _, die, rc, _ in _waited.values()),
   str({k: v[2] for k, v in _waited.items()}))
ck("and its own output", all(saw for _, _, _, saw in _waited.values()),
   str({k: v[3] for k, v in _waited.items()}))
_deadline = time.time() + 120
while time.time() < _deadline:
    _st = json.loads(open(sandbox.STATE).read())
    if len(_st["jobs"]) == 8 and all(j.get("state") not in ("running", "queued", None)
                                     for j in _st["jobs"].values()):
        break
    time.sleep(1)
_st = json.loads(open(sandbox.STATE).read())
ck("all eight jobs are recorded and finished", len(_st["jobs"]) == 8
   and all(j.get("state") not in ("running", "queued", None) for j in _st["jobs"].values()),
   str({k: v.get("state") for k, v in _st["jobs"].items()}))
_inbox = _st.get("inbox", [])
ck("nothing is reported about the jobs their callers watched",
   not [e for e in _inbox if (e.get("job") or "").startswith("waited-")],
   str([e.get("job") for e in _inbox]))
_reported = {e.get("job"): e.get("session_id") for e in _inbox
             if (e.get("job") or "").startswith("detached-")}
ck("and every detached job is reported once", len(_reported) == 4, str(sorted(_reported)))
ck("to the agent that started it", _reported == _detached, str(_reported))
sandbox.kill_watchers(cc)
shutil.rmtree(_work, ignore_errors=True)
reset()

print()
print("=== the escape hatches still work ===")
reset()
for a in ("agent-A", "agent-B"):
    as_session(a, "start", "job-" + a, "--eta", "3h", "--monitor", "time", "--no-watch")
ck("ls shows every session's work", 
   len(json.loads(as_session("agent-A", "ls", "--json").stdout or "[]")) == 2)
ck("watch shows every session's work",
   "job-agent-B" in as_session("agent-A", "watch", "--once").stdout)
as_session(None, "config", "--set", "scope=all")
ck("scope=all puts them back on one statusline", bars("agent-A") == ["job-agent-A", "job-agent-B"],
   str(bars("agent-A")))
reset()
queue_crash("someones-job", "agent-Z")
as_session(None, "config", "--set", "scope=all")
ck("and scope=all shares the crashes too", "someones-job" in context("agent-Q"),
   context("agent-Q")[:80])
as_session(None, "config", "--reset")
reset()
for a in ("agent-A", "agent-B"):
    as_session(a, "start", "job-" + a, "--eta", "3h", "--monitor", "time", "--no-watch")
ck("and scope=session separates them again", bars("agent-A") == ["job-agent-A"], str(bars("agent-A")))
reset()

print()
print("=== settings changed by several sessions at once ===")
# Reading the config, changing one key in the copy and writing the whole file
# back is a lost update: two sessions doing it at the same moment each keep
# their own change and silently discard the other's.
KEYS = ["max_jobs", "bar_width", "min_interval_seconds", "min_duration_seconds",
        "keep_done_seconds", "auto_track_after_seconds", "blend_full_at",
        "prune_after_hours", "spinner_fps", "crash_handover_seconds"]
lost_total = 0
for trial in range(3):
    as_session(None, "config", "--reset")
    want = {k: 11 + i for i, k in enumerate(KEYS)}
    th = [threading.Thread(target=as_session,
                           args=(("w%d" % i), "config", "--set", "%s=%d" % (k, want[k])))
          for i, k in enumerate(KEYS)]
    [t.start() for t in th]
    [t.join() for t in th]
    cfg = json.loads(as_session(None, "config", "--json").stdout)
    lost_total += sum(1 for k, v in want.items() if cfg.get(k) != v)
ck("no setting is lost to a racing writer", lost_total == 0,
   "%d lost over 3 trials" % lost_total)
as_session(None, "config", "--reset")

print()
print("=== several jobs dying together ===")
# A machine that runs out of memory, or a GPU that falls over, takes every job
# on it at once. Handing those over one at a time stopped the turn once per
# death, each with a single obituary.
reset()
for i in range(3):
    queue_crash("died-%d" % i, "s-batch")


def stop(sid, active=False):
    out = subprocess.run([sys.executable, os.path.join(HOOKS, "inject_status.py"), "Stop"],
                         input=json.dumps({"session_id": sid, "stop_hook_active": active}),
                         capture_output=True, text=True,
                         env=dict(os.environ, CLAUDE_CODE_SESSION_ID=sid)).stdout
    try:
        return json.loads(out or "{}")
    except ValueError:
        return {}


def side(result):
    return result.get("systemMessage") or result.get("reason") or ""


first = stop("s-batch")
ck("three deaths are shown together, not one interruption each",
   len(named(side(first), ["died-%d" % i for i in range(3)])) == 3, side(first)[:90])
ck("and none of it holds the turn open", first.get("decision") != "block", str(first)[:70])
ck("a second stop has nothing left to say", not side(stop("s-batch")))

reset()
for i in range(7):
    queue_crash("many-%d" % i, "s-many")
r = stop("s-many")
ck("more deaths than fit are capped",
   len(named(side(r), ["many-%d" % i for i in range(7)])) == 3,
   "named %d" % len(named(side(r), ["many-%d" % i for i in range(7)])))
ck("but it says how many are still queued", "more" in side(r).lower(), side(r)[-90:])
reset()
queue_crash("solo", "s-one")
r = stop("s-one")
ck("and a single death says nothing about others",
   "more tracked job" not in (r.get("reason") or ""))
ck("a stop that is already a stop hook never blocks",
   stop("s-one", active=True).get("decision") != "block")
reset()

print()
print("=== six agents running real jobs at once ===")
# Everything above uses jobs that stand still. This runs six real processes
# through the real hook, with their watchers writing to the shared state file
# at the same time, half of them dying - which is where cross-talk and a torn
# state file would actually show up.
reset()
import shutil
import tempfile
work = tempfile.mkdtemp(prefix="agent-progress-stress-")
open(os.path.join(work, "train.py"), "w").write(
    "import sys, time\n"
    "n = int(sys.argv[1]); die = sys.argv[2] == 'die'\n"
    "for i in range(1, n + 1):\n"
    "    print('Epoch %d/%d' % (i, n), flush=True); time.sleep(0.7)\n"
    "if die: raise MemoryError('boom')\n")
N = 6
AUTO = os.path.join(HOOKS, "auto_track.py")


def launch(i):
    """Start job i, detached, as session i.

    `run` rather than the wrapper: a report is only made for work that outlives
    the call that started it. A command the caller waits for hands back its own
    output and exit code, so there is nothing left to route - and routing is
    what this checks."""
    sid = "stress-%d" % i
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID=sid)
    subprocess.run([sys.executable, ENGINE, "run", "--name", "job-%d" % i,
                    "--eta", "1h", "--interval", "1s", "--cwd", work, "--",
                    sys.executable, os.path.join(work, "train.py"), "12",
                    "die" if i % 2 else "live"],
                   capture_output=True, env=env)


th = [threading.Thread(target=launch, args=(i,)) for i in range(N)]
[t.start() for t in th]
[t.join() for t in th]
for i in range(N):
    as_session("stress-%d" % i, "update", "job-%d" % i, "--eta", "1h", "--interval", "1s", "--quiet")
mixed = {i: bars("stress-%d" % i) for i in range(N)}
ck("each agent sees exactly its own live job",
   all(mixed[i] == ["job-%d" % i] for i in range(N)), str(mixed))

# generous: these wait on real watchers, and the whole suite run has the
# machine busy - a deadline tight enough to fail under load is a flaky
# test, not a finding
deadline = time.time() + 180
while time.time() < deadline:
    jobs = json.loads(open(sandbox.STATE).read())["jobs"]
    if len(jobs) == N and all(j.get("state") not in ("running", "queued") for j in jobs.values()):
        break
    time.sleep(1)
jobs = json.loads(open(sandbox.STATE).read())["jobs"]
ck("all six finished and none were lost", len(jobs) == N, str(sorted(jobs)))
ck("three succeeded and three died",
   sorted(j["state"] for j in jobs.values()) == ["done"] * 3 + ["failed"] * 3,
   str(sorted(j["state"] for j in jobs.values())))
ck("every job kept its own owner",
   len({j.get("session_id") for j in jobs.values()}) == N)

got = {}


def collect(i):
    sid = "stress-%d" % i
    got[sid] = named(context(sid), ["job-%d" % k for k in range(N)])


th = [threading.Thread(target=collect, args=(i,)) for i in range(N)]
[t.start() for t in th]
[t.join() for t in th]
# Both endings are reported now, so every agent hears about its own job and
# only its own - three of them that it crashed, three that it finished.
ck("and every ending went to the agent whose job it was",
   all(got["stress-%d" % i] == ["job-%d" % i] for i in range(N)), str(got))
_kinds = {}
for _e in json.loads(open(sandbox.STATE).read())["inbox"]:
    _kinds[_e.get("kind")] = _kinds.get(_e.get("kind"), 0) + 1
ck("three were crashes and three were finishes",
   _kinds.get("crash") == 3 and _kinds.get("done") == 3, str(_kinds))
sandbox.kill_watchers(cc)
shutil.rmtree(work, ignore_errors=True)
reset()

print()
print("=== two agents, one name: the refusal points at your own job ===")
reset()
as_session("agent-A", "start", "train", "--eta", "3h", "--monitor", "time", "--no-watch")
as_session("agent-B", "start", "train", "--eta", "3h", "--monitor", "time", "--no-watch")
r = as_session("agent-B", "cancel", "train")
ck("agent B saying `cancel train` is still refused", r.returncode != 0, str(r.returncode))
ck("but the refusal names B's own job first", "train-2" in r.stderr
   and r.stderr.index("train-2") < r.stderr.index("--any-session"), r.stderr[-240:])
r = as_session("agent-C", "cancel", "train")
ck("an agent with no job of that name gets no such hint", "did you mean" not in r.stderr,
   r.stderr[-240:])
ck("and nothing was cancelled",
   all(j["state"] == "running" for j in json.loads(open(sandbox.STATE).read())["jobs"].values()))
reset()

print()
print("=== a finished job's id is reused only by its own session ===")
reset()
as_session("agent-A", "start", "train", "--eta", "3h", "--monitor", "time", "--no-watch")
as_session("agent-A", "fail", "train", "--exit-code", "9")
as_session("agent-B", "start", "train", "--eta", "3h", "--monitor", "time", "--no-watch")
raw = json.loads(open(sandbox.STATE).read())["jobs"]
ck("another session's finished record is not replaced",
   raw.get("train", {}).get("session_id") == "agent-A" and raw["train"]["state"] == "failed",
   str({k: (v.get("session_id"), v.get("state")) for k, v in raw.items()}))
ck("the newcomer gets its own id", raw.get("train-2", {}).get("session_id") == "agent-B",
   str(sorted(raw)))
ck("so A still sees its crash on the bar", "train" in bars("agent-A"), str(bars("agent-A")))
as_session("agent-A", "start", "train", "--eta", "3h", "--monitor", "time", "--no-watch")
raw = json.loads(open(sandbox.STATE).read())["jobs"]
ck("while a session re-running its own finished job reuses the id",
   raw["train"]["session_id"] == "agent-A" and raw["train"]["state"] == "running",
   str((raw["train"].get("session_id"), raw["train"]["state"])))
reset()

print()
print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)
