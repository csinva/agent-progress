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


def bars(sid):
    """The job names on that session's statusline."""
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID=sid)
    out = subprocess.run([sys.executable, ENGINE, "statusline"],
                         input=json.dumps({"session_id": sid}),
                         capture_output=True, text=True, env=env).stdout
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
    out = subprocess.run([sys.executable, INJECT, "UserPromptSubmit"],
                         input=json.dumps({"session_id": sid}),
                         capture_output=True, text=True,
                         env=dict(os.environ, CLAUDE_CODE_SESSION_ID=sid)).stdout
    return json.loads(out or "{}").get("hookSpecificOutput", {}).get("additionalContext", "")


def reset():
    as_session(None, "rm", "--all")
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
ctx_a, ctx_b = context("agent-A"), context("agent-B")
ck("agent A hears about its own job", "job-agent-A" in ctx_a, ctx_a[:90])
ck("and not about agent B's", "job-agent-B" not in ctx_a, ctx_a[:90])
ck("agent B hears about its own", "job-agent-B" in ctx_b, ctx_b[:90])
ck("and not about agent A's", "job-agent-A" not in ctx_b, ctx_b[:90])

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
        got[a] = sorted(re.findall(r"'(job-agent-\d)'", text))
    right = all(got[a] == ["job-" + a] for a in agents)
    ck("trial %d: four crashes, four owners" % (trial + 1), right, str(got))

print()
print("=== nothing is lost when the owner has gone ===")
reset()
queue_crash("abandoned", "an-agent-that-exited", ts=time.time() - cc.ORPHAN_GRACE - 60)
ck("a stale crash is eventually offered to whoever is here",
   "abandoned" in context("someone-else"), context("someone-else")[:90])
reset()
queue_crash("fresh", "an-agent-still-running")
ck("but a fresh one is not taken from its owner",
   "fresh" not in context("someone-else"), context("someone-else")[:90])
ck("and its owner still gets it", "fresh" in context("an-agent-still-running"))

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
            seen[a] = (rows, sorted(re.findall(r"'(crash-agent-\d+)'", text)))

    th = [threading.Thread(target=look, args=(a,)) for a in agents]
    [t.start() for t in th]
    [t.join() for t in th]
    bad_bars = {a: v[0] for a, v in seen.items() if v[0] != ["job-" + a]}
    bad_crash = {a: v[1] for a, v in seen.items() if v[1] != ["crash-" + a]}
    ck("trial %d: 12 agents concurrently, bars stay put" % (trial + 1),
       not bad_bars, str(dict(list(bad_bars.items())[:3])))
    ck("trial %d: 12 agents concurrently, crashes stay put" % (trial + 1),
       not bad_crash, str(dict(list(bad_crash.items())[:3])))

print()
print("=== a job started through the hook belongs to the session that ran it ===")
reset()
AUTO = os.path.join(HOOKS, "auto_track.py")
for sid in ("agent-X", "agent-Y"):
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID=sid)
    out = subprocess.run([sys.executable, AUTO], env=env, capture_output=True, text=True,
                         input=json.dumps({"tool_name": "Bash", "session_id": sid,
                                           "tool_input": {"command": "python3 train.py"}})).stdout
    wrapped = json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"]
    wrapped = wrapped.replace("--after 20", "--after 1").replace(
        "--name train", "--name train-" + sid)
    subprocess.run(["/bin/sh", "-c", wrapped.replace("python3 train.py", "sleep 12")],
                   capture_output=True, env=env)
raw = json.loads(open(sandbox.STATE).read())["jobs"]
owners = {k: v.get("session_id") for k, v in raw.items()}
ck("the wrapped job records the session that ran it",
   owners.get("train-agent-X") == "agent-X" and owners.get("train-agent-Y") == "agent-Y",
   str(owners))
for sid in ("agent-X", "agent-Y"):        # long enough to earn a bar
    as_session(sid, "update", "train-" + sid, "--eta", "3h", "--quiet")
ck("and each agent sees only its own", bars("agent-X") == ["train-agent-X"], str(bars("agent-X")))
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
as_session("agent-A", "rm", "--all", "--everywhere")
ck("--everywhere still clears the lot",
   not json.loads(open(sandbox.STATE).read())["jobs"])
as_session("agent-A", "start", "solo", "--eta", "3h", "--monitor", "time", "--no-watch")
as_session(None, "rm", "--all")
ck("and from a plain shell it means everything",
   not json.loads(open(sandbox.STATE).read())["jobs"])

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
as_session(None, "config", "--reset")
ck("and scope=session separates them again", bars("agent-A") == ["job-agent-A"], str(bars("agent-A")))
reset()

print()
print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)
