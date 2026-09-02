#!/usr/bin/env python3
"""Hook and statusline contract tests.

Claude Code reads these outputs, so their shape matters as much as their
content: a statusline that emits an extra line, or a Stop hook that blocks
twice, misbehaves in ways the CLI never would.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "scripts", "agent_progress.py")
HOOKS = os.path.join(ROOT, "hooks")
STATUS = os.path.join(HOOKS, "inject_status.py")
AUTO = os.path.join(HOOKS, "auto_track.py")
# a state directory of this test run's own; must precede loading the engine,
# which reads AGENT_PROGRESS_HOME once at import time
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


def cli(*a, **kw):
    return subprocess.run([sys.executable, ENGINE] + list(a),
                          capture_output=True, text=True, **kw)


def hook(script, event, payload):
    return subprocess.run([sys.executable, script, event], input=json.dumps(payload),
                          capture_output=True, text=True).stdout.strip()


def statusline(payload):
    return subprocess.run([sys.executable, ENGINE, "statusline"], input=json.dumps(payload),
                          capture_output=True, text=True).stdout


cli("rm", "--all", "--force")
cli("config", "--reset")
with cc.state_rw() as st:
    st["inbox"] = []

print("=== the statusline stays inside its own lines ===")
cli("start", "one", "--eta", "2h", "--monitor", "time", "--no-watch",
    "--note", "first line\nsecond line\rthird")
out = statusline({})
ck("a note containing newlines does not add rows", out.count("\n") <= 1,
   "%d rows: %r" % (out.count("\n"), out[:120]))
cli("update", "one", "--desc", "a\nb", "--quiet")
ck("a description with newlines is contained",
   statusline({}).count("\n") <= 1, repr(statusline({})[:120]))
cli("rm", "--all", "--force")

for n in range(5):
    cli("start", "job%d" % n, "--eta", "2h", "--monitor", "time", "--no-watch")
out = statusline({})
ck("never more rows than max_jobs plus the overflow note",
   len([l for l in out.splitlines() if l.strip()]) <= 4,
   "%d rows" % len(out.splitlines()))
narrow = statusline({"terminal_width": 40})
ck("honours the terminal width",
   all(cc.visible_len(l) <= 40 for l in narrow.splitlines() if l.strip()),
   str([cc.visible_len(l) for l in narrow.splitlines()]))
ck("statusline emits no ansi when colour is off",
   "\033[" not in subprocess.run([sys.executable, ENGINE, "statusline"], input="{}",
                                 capture_output=True, text=True,
                                 env=dict(os.environ, NO_COLOR="1")).stdout)
cli("rm", "--all", "--force")

print()
print("=== the Stop hook ===")
cli("config", "--reset")
subprocess.run([sys.executable, ENGINE, "exec", "--after", "1s", "--name", "dies",
                "--shell", "sleep 2; exit 4"], capture_output=True)
deadline = time.time() + 120
while time.time() < deadline:
    jobs = json.loads(cli("ls", "--json").stdout or "[]")
    if jobs and jobs[0]["state"] != "running":
        break
    time.sleep(1)
# A turn ending is the user's turn to speak. News about a job goes beside the
# transcript, where it interrupts nobody, and never holds the turn open.
first = hook(STATUS, "Stop", {"session_id": "s", "stop_hook_active": False})
_first = json.loads(first or "{}")
ck("a crash is put in front of the user when the turn ends",
   "systemMessage" in _first, first[:90])
ck("and it says which job and why",
   "dies" in _first.get("systemMessage", ""), _first.get("systemMessage", "")[:70])
ck("without holding the turn open", _first.get("decision") != "block", first[:90])
second = hook(STATUS, "Stop", {"session_id": "s", "stop_hook_active": False})
ck("and only once", second == "", second[:100])
with cc.state_rw() as st:
    for e in st.get("inbox", []):
        e["delivered"] = None
guarded = hook(STATUS, "Stop", {"session_id": "s", "stop_hook_active": True})
ck("never blocks when already inside a stop hook", guarded == "", guarded[:100])
cli("config", "--set", "crash_alert=false")
off = hook(STATUS, "Stop", {"session_id": "s", "stop_hook_active": False})
ck("crash_alert=false silences the interruption", off == "", off[:100])
cli("config", "--reset")
cli("rm", "--all", "--force")
with cc.state_rw() as st:
    st["inbox"] = []

print()
print("=== SessionStart brings back a lost watcher ===")
cli("start", "orphan", "--eta", "2h", "--monitor", "time", "--no-watch")
with cc.state_rw() as st:
    st["jobs"]["orphan"]["watcher_pid"] = 999999      # a pid that cannot exist
hook(STATUS, "SessionStart", {"session_id": "s"})
time.sleep(2)
pid = json.loads(open(cc.STATE).read())["jobs"]["orphan"].get("watcher_pid")
ck("a dead watcher is replaced", pid != 999999 and cc.alive(pid), "watcher_pid=%s" % pid)
cli("rm", "--all", "--force")
time.sleep(2)

print()
print("=== a state directory whose path contains spaces ===")
spacey = os.path.join(tempfile.mkdtemp(prefix="agent progress "), "state dir")
env = dict(os.environ, AGENT_PROGRESS_HOME=spacey)
r = subprocess.run([sys.executable, ENGINE, "exec", "--after", "1s", "--shell",
                    "echo hello from a spacey path"], capture_output=True, text=True, env=env)
ck("a fast command works from a path with spaces",
   r.returncode == 0 and "hello from a spacey path" in r.stdout, repr(r.stdout[:80]))
r = subprocess.run([sys.executable, ENGINE, "exec", "--after", "1s", "--name", "spacey",
                    "--shell", "echo a; sleep 3; echo b"], capture_output=True, text=True, env=env)
ck("a handoff works from a path with spaces", "tracked as" in r.stdout, repr(r.stdout[:120]))
jobs = json.loads(subprocess.run([sys.executable, ENGINE, "ls", "--json"],
                                 capture_output=True, text=True, env=env).stdout or "[]")
ck("the job's log is readable there", jobs and os.path.exists(jobs[0]["log"] or ""), str(jobs))
time.sleep(3)
shutil.rmtree(os.path.dirname(spacey), ignore_errors=True)

print()
print("=== reporting commands ===")
cli("rm", "--all", "--force")
cli("start", "running-one", "--eta", "2h", "--monitor", "time", "--no-watch")
cli("start", "finished-one", "--eta", "2h", "--monitor", "time", "--no-watch")
cli("done", "finished-one")
ck("ls --running shows only what is running",
   len(json.loads(cli("ls", "--json", "--running").stdout or "[]")) == 1)
d = json.loads(cli("show", "running-one", "--json").stdout or "{}")
ck("show --json carries the estimate", "estimate" in d and d.get("id") == "running-one",
   str(list(d)[:6]))
ck("inbox --limit is accepted", cli("inbox", "--limit", "2").returncode == 0)
cli("rm", "--all", "--force")

print()
print("=== a working directory that is not there ===")
for label, args in [("exec", ["exec", "--cwd", "/no/such/dir", "--shell", "echo hi"]),
                    ("run", ["run", "--name", "cw", "--cwd", "/no/such/dir", "--", "echo hi"])]:
    r = cli(*args)
    ck("%s reports a missing --cwd cleanly" % label,
       r.returncode != 0 and "Traceback" not in r.stderr and "no such directory" in r.stderr,
       r.stderr.strip()[-90:])
cli("rm", "--all", "--force")

print()
print("=== loading into a session that was already running ===")
cli("rm", "--all", "--force")
with cc.state_rw() as st:
    st["sessions"] = {}

env_old = dict(os.environ, CLAUDE_CODE_SESSION_ID="was-already-open")
r = subprocess.run([sys.executable, ENGINE, "start", "s1", "--eta", "3h",
                    "--monitor", "time", "--no-watch"],
                   capture_output=True, text=True, env=env_old)
warned = "started before agent-progress was loaded" in r.stdout
ck("a job from an older session explains the missing bar",
   warned or not cc.statusline_wired(), r.stdout[-120:])
ck("doctor says which kind of session this is",
   "started BEFORE" in subprocess.run([sys.executable, ENGINE, "doctor"],
                                      capture_output=True, text=True,
                                      env=env_old).stdout or not cc.statusline_wired())
hook(STATUS, "SessionStart", {"session_id": "was-already-open"})
r2 = subprocess.run([sys.executable, ENGINE, "start", "s2", "--eta", "3h",
                     "--monitor", "time", "--no-watch"],
                    capture_output=True, text=True, env=env_old)
ck("and stops saying it once the session is known",
   "started before agent-progress was loaded" not in r2.stdout, r2.stdout[-120:])
ck("session_is_new is false for a recorded session",
   not cc.session_is_new("was-already-open"))
ck("session_is_new is true for one never seen", cc.session_is_new("never-seen-before"))
ck("no session id means no claim either way", not cc.session_is_new(None))
cli("rm", "--all", "--force")

print()
print("=== the wrapped command does not depend on PATH ===")
launcher = cc.launcher_prefix()
ck("the launcher is an absolute path", launcher.strip("'").startswith("/"), launcher)
wrapped = cc.wrap_command("echo ran", "t", after=20)
for label, path in [("without ~/.local/bin",
                     ":".join(d for d in os.environ["PATH"].split(":")
                              if not d.endswith("/.local/bin"))),
                    ("with no PATH at all", "")]:
    r = subprocess.run(["/bin/sh", "-c", wrapped], capture_output=True, text=True,
                       env=dict(os.environ, PATH=path))
    ck("a rewritten command still runs %s" % label,
       r.returncode == 0 and "ran" in r.stdout, "exit=%d %s" % (r.returncode, r.stderr[:50]))

print()
print("=== a long job asked for in words never blocks ===")
cli("rm", "--all", "--force")
cli("config", "--reset")
scratch = tempfile.mkdtemp(prefix="agent-progress-words-")
open(os.path.join(scratch, "train.py"), "w").write(
    "import time\nfor i in range(1, 40):\n    print('Epoch %d/40' % i, flush=True)\n"
    "    time.sleep(1)\n")

# the shapes a model actually picks when told "run training"
for label, cmd in [
        ("a script", "python3 train.py --epochs 40"),
        ("a module", "python3 -m src.train --config base.yaml"),
        ("a shell script", "bash scripts/train.sh"),
        ("a flag", "python3 main.py --mode train")]:
    out = hook(AUTO, "PreToolUse", {"tool_name": "Bash", "session_id": "words",
                                    "tool_input": {"command": cmd}})
    ck("caught when Claude runs %s" % label, bool(out), cmd)

# and the job really does come back long before it finishes
real = "python3 %s" % os.path.join(scratch, "train.py")
out = hook(AUTO, "PreToolUse", {"tool_name": "Bash", "session_id": "words",
                                "tool_input": {"command": real}})
wrapped = json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"]
wrapped = wrapped.replace("--after 20", "--after 2")
t = time.time()
r = subprocess.run(["/bin/sh", "-c", wrapped], capture_output=True, text=True)
elapsed = time.time() - t
ck("a 40s job returns in about the threshold, not 40s", elapsed < 8,
   "%.1fs" % elapsed)
ck("and it is still running afterwards",
   any(j["state"] == "running" for j in json.loads(cli("ls", "--json").stdout or "[]")),
   cli("ls", "--json").stdout[:80])
ck("the caller is told in a few lines, not a wall of text",
   len(r.stdout.strip().splitlines()) <= 12, "%d lines" % len(r.stdout.strip().splitlines()))
cli("rm", "--all", "--force")

# the explicit route a model should prefer when it knows the job is long
t = time.time()
r = cli("run", "--name", "explicit", "--eta", "3h", "--", "sleep", "60")
ck("an explicit run returns at once", time.time() - t < 5, "%.1fs" % (time.time() - t))
d = json.loads(cli("ls", "--json").stdout or "[]")
ck("and its bar has an eta from the first frame",
   d and d[0]["remaining_s"] and d[0]["remaining_s"] > 3000, str(d and d[0].get("remaining_human")))
cli("cancel", "explicit")
cli("rm", "--all", "--force")
shutil.rmtree(scratch, ignore_errors=True)

print()
print("=== the wrapper never wraps itself ===")


def wrapped_form(cmd):
    out = hook(AUTO, "PreToolUse", {"tool_name": "Bash", "session_id": "recur",
                                    "tool_input": {"command": cmd}})
    return json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"] if out else None


once = wrapped_form("python3 train.py")
ck("a real command is wrapped", bool(once), str(once))
ck("its wrapped form is not wrapped again", wrapped_form(once) is None, str(wrapped_form(once)))
for form in ["agent-progress ls",
             os.path.expanduser("~/.local/bin/agent-progress") + " exec --shell 'python3 train.py'",
             "sudo /usr/local/bin/agent-progress run -- python3 train.py",
             "python3 /somewhere/agent_progress.py exec --shell 'python3 train.py'"]:
    ck("left alone: %s" % form[:44], wrapped_form(form) is None)

print()
print("=== the session a job belongs to is recorded ===")
cli("rm", "--all", "--force")
for name, sid in [("other", "sessB"), ("mine", "sessA")]:
    subprocess.run([sys.executable, ENGINE, "start", name, "--eta", "3h",
                    "--monitor", "time", "--no-watch"], capture_output=True,
                   env=dict(os.environ, CLAUDE_CODE_SESSION_ID=sid))
    time.sleep(0.1)
raw = json.loads(open(cc.STATE).read())["jobs"]
ck("a job records the session that started it",
   raw["mine"]["session_id"] == "sessA" and raw["other"]["session_id"] == "sessB",
   str({k: v.get("session_id") for k, v in raw.items()}))
out = statusline({"session_id": "sessA"})
names = [re.sub(r"\x1b\[[0-9;]*m", "", l).split()[1] for l in out.splitlines() if l.strip()]
ck("this session's jobs come first", names and names[0] == "mine", str(names))
cli("rm", "--all", "--force")

print()
print("=== a clock that jumped ===")
future = {"id": "future", "state": "running", "started": time.time() + 600,
          "unit": "it", "samples": [], "eta_end": time.time() + 1200}
cfg = cc.load_config()
ck("a job that appears to start in the future still renders",
   isinstance(cc.render_line(future, cfg, width=100), str))
ck("and its elapsed time is not negative",
   "-" not in cc.fmt_dur(cc.estimate(future, None, cfg)["elapsed"]))

print()
print()
print("=== a watcher that dies mid-job comes back ===")
# Watchers were revived only when a session started. Inside a session left open
# for hours, nothing ever brought one back: the job's record stopped being
# updated and its bar sat at whatever it last said, still calling a job that
# had finished long ago "running".
cli("rm", "--all", "--force")
orphan = "orphaned-" + sandbox.TAG
cli("run", "--name", orphan, "--eta", "1h", "--", "sleep", "30")
time.sleep(2)
wpid = json.loads(open(cc.STATE).read())["jobs"][orphan].get("watcher_pid")
ck("the job started with a watcher", bool(wpid))
try:
    os.kill(int(wpid), 9)
except OSError:
    pass
time.sleep(1)
gone = subprocess.run(["pgrep", "-f", "_watch " + orphan], capture_output=True, text=True).stdout.split()
ck("and the watcher is gone", not gone, str(gone))
hook(STATUS, "UserPromptSubmit", {"session_id": "s"})
time.sleep(1.5)
back = subprocess.run(["pgrep", "-f", "_watch " + orphan], capture_output=True, text=True).stdout.split()
ck("a prompt brings it back", len(back) == 1, str(back))
hook(STATUS, "UserPromptSubmit", {"session_id": "s"})
time.sleep(1)
again = subprocess.run(["pgrep", "-f", "_watch " + orphan], capture_output=True, text=True).stdout.split()
ck("and does not stack up a second one", len(again) == 1, str(again))
sandbox.kill_watchers(cc)
subprocess.run(["pkill", "-f", "_watch " + orphan], capture_output=True)
cli("rm", "--all", "--force")

print()
print("=== a finished bar retires after a couple of messages ===")
# A completed bar is there to be noticed, not to be lived with, and time alone
# measured that badly: five minutes is many messages if you are working and none
# at all if you stepped away.
cli("rm", "--all", "--force")


# the jobs below are made by cli(), which runs with whatever session this test
# process is in, so the bar has to be drawn for that same session
_ME = cc.current_session()


def _bar():
    out = subprocess.run([sys.executable, ENGINE, "statusline"],
                         input=json.dumps({"session_id": _ME}),
                         capture_output=True, text=True, env=os.environ).stdout
    return re.sub(r"\033\[[0-9;]*m", "", out)


def _prompt(sid=None):
    env = dict(os.environ)
    if sid:
        env["CLAUDE_CODE_SESSION_ID"] = sid
    subprocess.run([sys.executable, STATUS, "UserPromptSubmit"],
                   input=json.dumps({"session_id": sid or _ME}),
                   capture_output=True, text=True, env=env)


cli("start", "finished", "--eta", "2h", "--monitor", "time", "--no-watch")
cli("done", "finished")
ck("it is shown the moment it finishes", "finished" in _bar())
_prompt()
ck("and still after one message", "finished" in _bar())
_prompt()
ck("but not after two", "finished" not in _bar(), _bar()[:60])

cli("rm", "--all", "--force")
cli("start", "crashed", "--eta", "2h", "--monitor", "time", "--no-watch")
cli("fail", "crashed", "--note", "exploded")
for _ in range(4):
    _prompt()
ck("a crash is not retired that quickly", "crashed" in _bar(), _bar()[:60])

cli("rm", "--all", "--force")
cli("start", "running-still", "--eta", "2h", "--monitor", "time", "--no-watch")
for _ in range(4):
    _prompt()
ck("and a running job is untouched", "running-still" in _bar(), _bar()[:60])

cli("rm", "--all", "--force")
# a name of its own: an earlier test leaves a job called "mine" behind, owned by
# another session, so reusing the name here got this one called "mine-2" and the
# `done` refused - correctly - as reaching into somebody else's session
cli("start", "my-own-job", "--eta", "2h", "--monitor", "time", "--no-watch")
cli("done", "my-own-job")
for _ in range(5):
    _prompt("a-different-session")
ck("another session's messages do not retire yours", "my-own-job" in _bar(), _bar()[:60])
_prompt()
_prompt()
ck("your own two do", "my-own-job" not in _bar(), _bar()[:60])
cli("rm", "--all", "--force")

print()
print("=== a job that finishes hands its result back ===")
# A handed-off command writes its output to a log the session never reads. If
# nothing says it finished, and nothing gives back what it produced, asking for
# a model to be trained means never being told the answer.
cli("rm", "--all", "--force")
with cc.state_rw() as st:
    st["inbox"] = []
_prog = os.path.join(sandbox.HOME, "produces.py")
open(_prog, "w").write(
    "import time\n"
    "for i in range(3):\n"
    "    print('step %d/3' % (i + 1), flush=True); time.sleep(1)\n"
    "print('FINAL RESULT: accuracy 0.93')\n")
cli("run", "--name", "produces", "--eta", "1h", "--", sys.executable, _prog)
_final = None
for _ in range(40):
    time.sleep(1)
    _final = json.loads(open(cc.STATE).read())["jobs"].get("produces", {}).get("state")
    if _final not in ("running", None):
        break
ck("it finished", _final == "done", str(_final))
# The result comes back beside the conversation, not inside it: the user sees
# what their job produced, and nothing is added to what they said to Claude.
_side = json.loads(hook(STATUS, "Stop",
                        {"session_id": cc.current_session(),
                         "stop_hook_active": False}) or "{}").get("systemMessage", "")
ck("the user is shown that it finished", "finished" in _side.lower(), _side[:80])
ck("and the result it produced", "FINAL RESULT: accuracy 0.93" in _side, _side[-120:])
ck("and where to read more of it", "agent-progress log" in _side, _side[-60:])
_ctx = hook(STATUS, "UserPromptSubmit", {"session_id": cc.current_session()})
_text = json.loads(_ctx or "{}").get("hookSpecificOutput", {}).get("additionalContext", "")
ck("nothing rides along with the user's next message", not _text.strip(), _text[:60])
_again = json.loads(hook(STATUS, "Stop", {"session_id": cc.current_session(),
                                          "stop_hook_active": False}) or "{}")
ck("and it is not shown twice", "finished" not in _again.get("systemMessage", "").lower(),
   str(_again)[:60])

cli("rm", "--all", "--force")
with cc.state_rw() as st:
    st["inbox"] = []
cli("config", "--set", "announce_done=false")
cli("run", "--name", "quiet-one", "--eta", "1h", "--", "sh", "-c", "echo hi")
for _ in range(30):
    time.sleep(1)
    if json.loads(open(cc.STATE).read())["jobs"].get("quiet-one", {}).get("state") != "running":
        break
_text = json.loads(hook(STATUS, "UserPromptSubmit", {"session_id": cc.current_session()}) or "{}")
_text = _text.get("hookSpecificOutput", {}).get("additionalContext", "")
ck("announce_done=false keeps it quiet", "FINISHED" not in _text, _text[:60])
cli("config", "--reset")
sandbox.kill_watchers(cc)
cli("rm", "--all", "--force")

print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)
