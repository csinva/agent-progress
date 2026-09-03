#!/usr/bin/env python3
"""Unit tests for agent-progress's internals.

run_tests.py checks that the CLI behaves; this checks the parts underneath it -
duration and size parsing, progress scraping, the estimator, line clipping, the
monitors, and config coercion. These are where a wrong answer is quiet: a bar
that is subtly wrong looks like a bar.
"""
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import time

ENGINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "agent_progress.py")
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


def eq(name, got, want):
    ck(name, got == want, "got %r want %r" % (got, want))


print("=== parse_duration ===")
for text, want in [("45m", 2700), ("2h30m", 9000), ("90s", 90), ("1h", 3600),
                   ("2:30:00", 9000), ("120", 120), ("0", 0), ("1.5h", 5400),
                   ("1h30", 3630), ("30", 30), ("", None), (None, None)]:
    eq("parse_duration(%r)" % text, cc.parse_duration(text), want)
try:
    cc.parse_duration("banana")
    ck("parse_duration rejects nonsense", False, "no error raised")
except SystemExit:
    ck("parse_duration rejects nonsense", True)

print()
print("=== parse_size / fmt_size ===")
for text, want in [("12GB", 12 * 1024**3), ("500 MB", 500 * 1024**2), ("1.5t", int(1.5 * 1024**4)),
                   ("4096", 4096), ("8mb", 8 * 1024**2), (None, None)]:
    eq("parse_size(%r)" % text, cc.parse_size(text), want)
try:
    cc.parse_size("big")
    ck("parse_size rejects nonsense", False)
except SystemExit:
    ck("parse_size rejects nonsense", True)
eq("fmt_size(0)", cc.fmt_size(0), "0B")
eq("fmt_size(1536)", cc.fmt_size(1536), "1.5KB")
eq("fmt_size(None)", cc.fmt_size(None), "?")

print()
print("=== duration formatting ===")
for secs, want in [(0, "00:00"), (59, "00:59"), (60, "01:00"), (3599, "59:59"),
                   (3600, "1:00:00"), (-5, "00:00"), (None, "--:--")]:
    eq("fmt_dur(%r)" % secs, cc.fmt_dur(secs), want)
for secs, want in [(0, "0s"), (59, "59s"), (60, "1m"), (3600, "1h"),
                   (3660, "1h01m"), (None, "?")]:
    eq("fmt_short(%r)" % secs, cc.fmt_short(secs), want)

print()
print("=== clipping keeps within the column budget ===")
line = cc.paint("\U0001f480 job", "fail", True) + " " + "x" * 200
for w in (1, 2, 3, 5, 10, 40, 120):
    got = cc.visible_len(cc.clip(line, w))
    ck("clip to %d columns" % w, got <= w, "produced %d columns" % got)
ck("clip leaves short lines alone", cc.clip("abc", 40) == "abc")
ck("clip closes colour codes", cc.clip(line, 10).endswith("\033[0m"))

print()
print("=== progress scraping ===")
cases = [
    ("tqdm", " 45%|####      | 45/100 [00:12<00:14]", 45, 100),
    ("epoch", "Epoch 12/50 - loss 0.3", 12, 50),
    ("step", "global_step 1200/10000", 1200, 10000),
    ("keras", "   32/1875 [>....]", 32, 1875),
    ("trial", "Trial 7 of 40 finished", 7, 40),
]
for name, text, step, total in cases:
    r = cc.parse_progress(text)
    ck("parse %s" % name, r and r["step"] == step and r["total"] == total, repr(r))
ck("percent only", (cc.parse_progress("67% complete") or {}).get("pct") == 0.67)
ck("ignores paths and versions",
   cc.parse_progress("loading /a/b/c-1/2 v1.2.3 at 2026/08/31") is None,
   repr(cc.parse_progress("loading /a/b/c-1/2 v1.2.3 at 2026/08/31")))
ck("ignores a step beyond its total", cc.parse_progress("weird 500/100") is None,
   repr(cc.parse_progress("weird 500/100")))
ck("survives a huge line", cc.parse_progress("x" * 200000 + " 5/10 ") is not None)
try:
    cc.parse_progress("5/10", pattern="(unclosed")
    ck("a bad custom pattern is survivable", True)
except Exception as ex:
    ck("a bad custom pattern is survivable", False, "%s: %s" % (type(ex).__name__, ex))

print()
print("=== probe output ===")
for text, want in [("42\n", 42), ("42/100\n", 42), ("  7 / 9 \n", 7), ("", None)]:
    r = cc.parse_probe_output(text)
    got = r.get("step") if r else None
    eq("probe %r" % text, got, want)
eq("probe percent", (cc.parse_probe_output("55%") or {}).get("pct"), 0.55)

print()
print("=== the estimator ===")
now = time.time()
cfg = cc.load_config()

def job(**kw):
    base = dict(id="j", state="running", started=now - 600, unit="it", samples=[])
    base.update(kw)
    return base

j = job(total=100, units=50.0, samples=[[now - 600 + i * 12, i] for i in range(51)])
e = cc.estimate(j, now, cfg)
ck("measured rate wins once there is data", e["source"] == "measured", e["source"])
ck("remaining is sane", 500 < e["remaining"] < 700, str(e["remaining"]))
e = cc.estimate(job(total=100, units=1.0, eta_end=now + 3000,
                    samples=[[now - 30, 0], [now - 10, 1]]), now, cfg)
ck("blends early on", e["source"] == "blend", e["source"])
e = cc.estimate(job(eta_end=now + 900, started=now - 900), now, cfg)
ck("falls back to the prior", e["source"] == "claude", e["source"])
ck("prior fraction never reaches 1", e["frac"] < 1.0, str(e["frac"]))

ck("zero total does not divide by zero", cc.estimate(job(total=0, units=5), now, cfg)["frac"] is None)
ck("units past total clamps to 1",
   cc.estimate(job(total=10, units=99.0), now, cfg)["frac"] == 1.0)
ck("negative step clamps to 0",
   cc.estimate(job(total=10, units=-5.0), now, cfg)["frac"] == 0.0)
e = cc.estimate(job(state="done", ended=now, total=10, units=10.0), now, cfg)
ck("finished job is 100% and has no remaining", e["frac"] == 1.0 and e["remaining"] == 0)
e2 = cc.estimate(job(state="failed", ended=now - 100, eta_end=now + 900), now, cfg)
e3 = cc.estimate(job(state="failed", ended=now - 100, eta_end=now + 900), now + 500, cfg)
ck("a stopped job's bar is frozen", e2["frac"] == e3["frac"], "%s vs %s" % (e2["frac"], e3["frac"]))
ck("no signal at all -> indeterminate", cc.estimate(job(), now, cfg)["frac"] is None)

print()
print("=== monitors ===")
scratch = tempfile.mkdtemp(prefix="agent-progress-unit-")
log = os.path.join(scratch, "j.log")
open(log, "w").write("loading data\nnormalizing (v2)\n")
m = cc.monitor_reading({"log": log, "log_offset": 0,
                        "monitor": {"kind": "milestones",
                                    "milestones": ["loading data", "normalizing (v2)",
                                                   "writing"]}}, now)
ck("milestones count what has appeared", m and m["step"] == 2, repr(m))
open(log, "a").write("cost was $5\n")
j2 = {"log": log, "log_offset": 0,
      "monitor": {"kind": "milestones", "milestones": ["cost was $5", "done"]}}
m = cc.monitor_reading(j2, now)
ck("a stage name with regex characters still matches", m and m["step"] == 1, repr(m))
j3 = {"log": log, "log_offset": 0,
      "monitor": {"kind": "milestones", "milestones": ["(unclosed", "loading data"]}}
m = cc.monitor_reading(j3, now)
ck("an unparseable stage name does not crash", m is not None, repr(m))

os.makedirs(os.path.join(scratch, "out"))
mon = {"kind": "files", "glob": os.path.join(scratch, "out", "*.txt"), "total": 3}
ck("files monitor with no matches yet",
   (cc.monitor_reading({"monitor": mon}, now) or {}).get("step") == 0)
open(os.path.join(scratch, "out", "a.txt"), "w").write("x")
ck("files monitor counts", (cc.monitor_reading({"monitor": mon}, now) or {}).get("step") == 1)

ck("size monitor with a missing path returns nothing",
   cc.monitor_reading({"monitor": {"kind": "size", "path": scratch + "/nope",
                                   "target_bytes": 100}}, now) is None)
open(os.path.join(scratch, "big"), "w").write("x" * 50)
m = cc.monitor_reading({"monitor": {"kind": "size", "path": os.path.join(scratch, "big"),
                                    "target_bytes": 100}}, now)
ck("size monitor measures", m and abs(m["pct"] - 0.5) < 0.01, repr(m))
m = cc.monitor_reading({"monitor": {"kind": "size", "path": scratch}}, now)
ck("size monitor without a target gives no fraction", m is None, repr(m))

ck("probe that fails is survivable",
   cc.monitor_reading({"monitor": {"kind": "probe", "cmd": "exit 7"}}, now) is None)
ck("probe that prints nothing is survivable",
   cc.monitor_reading({"monitor": {"kind": "probe", "cmd": "true"}}, now) is None)
m = cc.monitor_reading({"monitor": {"kind": "probe", "cmd": "echo 12", "total": 40}}, now)
ck("probe reads a count", m and m["step"] == 12, repr(m))
ck("probe that hangs is bounded",
   cc.monitor_reading({"monitor": {"kind": "probe", "cmd": "sleep 30", "timeout": 1}}, now) is None)
ck("time monitor reads nothing", cc.monitor_reading({"monitor": {"kind": "time"}}, now) is None)
ck("a bad custom pattern does not crash the monitor",
   cc.monitor_reading({"log": log, "log_offset": 0, "pattern": "(unclosed",
                       "monitor": {"kind": "log"}}, now) is None)
shutil.rmtree(scratch, ignore_errors=True)

print()
print("=== config coercion ===")
eq("bool from 'true'", cc.coerce("color", "true"), True)
eq("bool from 'off'", cc.coerce("color", "off"), False)
eq("int from '30'", cc.coerce("bar_width", "30"), 30)
eq("float", cc.coerce("interval_fraction", "0.25"), 0.25)
eq("wrap is still accepted", cc.coerce("auto_track", "wrap"), "defer")
for key, bad in [("bar_width", "9999"), ("style", "fancy"), ("color", "maybe"),
                 ("interval_fraction", "2.0"), ("bar_width", "abc")]:
    try:
        cc.coerce(key, bad)
        ck("%s=%s rejected" % (key, bad), False, "accepted")
    except ValueError:
        ck("%s=%s rejected" % (key, bad), True)

print()
print("=== command classification ===")
eq("name from a script", cc.suggest_job_name("python3 src/train_model.py --lr 1"), "train_model")
eq("name from a subcommand", cc.suggest_job_name("cargo build --release"), "build")
ck("name is never empty", cc.suggest_job_name("!!!") == "job")
ck("timeout given as text does not crash",
   cc.classify_command("./x", {"timeout": "600000"}, cfg) is not None)
ck("a command that is only whitespace is ignored",
   not cc.classify_command("   ", {}, cfg)["track"])

print()
print("=== crash reasons ===")
eq("exit 1", cc.crash_reason(1)[0], "exit 1")
eq("SIGKILL", cc.crash_reason(137)[0], "SIGKILL")
eq("SIGSEGV", cc.crash_reason(139)[0], "SIGSEGV")
eq("unknown signal", cc.crash_reason(128 + 60)[0], "signal 60")
ck("no code at all", cc.crash_reason(None)[0] == "no exit code")

print()
print("=== rendering with awkward settings ===")
import subprocess


def render(job, **over):
    c = dict(cc.load_config())
    c.update(over)
    return cc.render_line(job, c, width=120)


j = job(total=10, units=5.0)
plain = cc.visible_len(render(j, bar_width=20))
wide = cc.visible_len(render(j, fill_char="ab", bar_width=20))
ck("a multi-character fill does not widen the bar", wide == plain,
   "%d columns vs %d" % (wide, plain))
ck("an empty spinner does not crash", isinstance(render(j, spinner=""), str))
ck("colour off produces no escapes", "\033[" not in render(j, color=False))
ck("a wide glyph is accounted for",
   cc.visible_len(cc.clip(render(job(state="failed", ended=now, exit_code=137),
                                 glyph_failed="\U0001f480"), 30)) <= 30)

print()
print("=== the CLI on awkward input ===")


def run(*a, **kw):
    return subprocess.run([sys.executable, ENGINE] + list(a),
                          capture_output=True, text=True, **kw)


run("rm", "--all", "--force")
ck("log for an unknown job fails cleanly",
   run("log", "nope").returncode != 0 and "Traceback" not in run("log", "nope").stderr)
ck("show for an unknown job fails cleanly",
   "Traceback" not in run("show", "nope").stderr)
ck("rm for an unknown job fails cleanly",
   "Traceback" not in run("rm", "nope").stderr)
r = run("run", "--name", "edge", "--eta", "nonsense", "--", "true")
ck("a bad --eta is reported, not crashed",
   r.returncode != 0 and "Traceback" not in r.stderr, r.stderr[-120:])
r = run("run", "--name", "edge", "--pattern", "(unclosed", "--", "true")
ck("a bad --pattern is reported, not crashed",
   r.returncode != 0 and "not a valid regex" in (r.stderr + r.stdout), r.stderr[-120:])
r = run("preview", "--set", "bar_width=abc")
ck("preview rejects a bad value cleanly",
   "Traceback" not in r.stderr, r.stderr[-160:])
r = run("statusline", input="{}")
ck("statusline with no jobs is fine", r.returncode == 0)
run("start", "aaa-one", "--eta", "1h", "--monitor", "time", "--no-watch")
run("start", "aaa-two", "--eta", "1h", "--monitor", "time", "--no-watch")
r = run("show", "aaa")
ck("an ambiguous job name is reported, not crashed",
   "ambiguous" in (r.stderr + r.stdout).lower(), (r.stderr + r.stdout)[-120:])
run("done", "aaa-one")
r = run("done", "aaa-one")
ck("finishing an already finished job is harmless", r.returncode == 0, r.stderr[-120:])
run("rm", "--all", "--force")

print()
print()
print("=== a bar the threshold has already allowed is not taken back ===")
# Estimates move as a job is measured. One that starts at half an hour and is
# revised down to ninety seconds used to take its own bar away mid-run, then
# give it back once elapsed crossed the threshold on its own.
cfg = dict(cc.load_config())
cfg["min_duration_seconds"] = 120
now = time.time()
long_then_short = {"state": "running", "started": now - 20, "samples": [],
                   "est_total_s": 45, "initial_est_total_s": 1800,
                   "eta_end": now + 25, "unit": "it"}
ck("a job once thought long keeps its bar", cc.job_visible(long_then_short, cfg, now))
never_long = {"state": "running", "started": now - 20, "samples": [],
              "est_total_s": 20, "initial_est_total_s": 20,
              "eta_end": now, "unit": "it"}
ck("a job never thought long stays off the statusline",
   not cc.job_visible(never_long, cfg, now))
grown = {"state": "running", "started": now - 20, "samples": [],
         "est_total_s": 3600, "initial_est_total_s": 30, "eta_end": now + 3580, "unit": "it"}
ck("and one that turns out to be long earns a bar", cc.job_visible(grown, cfg, now))
old_enough = {"state": "running", "started": now - 300, "samples": [], "unit": "it"}
ck("elapsed alone is still enough", cc.job_visible(old_enough, cfg, now))

print()
print("=== every way of creating a job, in one breath ===")
# A search-and-replace once landed inside attach_batch_job instead of the two
# places it was meant for, and every scheduler job raised NameError on creation
# - no slurm, LSF or PBS job could be tracked at all. It shipped, because the
# paths that were checked were the ones the edit was supposed to touch. This
# costs a second and covers all of them.
import types as _types

_args = _types.SimpleNamespace(
    eta=None, note=None, desc=None, unit=None, total=None, pattern=None,
    monitor=None, log=None, cwd=None, interval=None, force_show=False,
    files=None, size=None, probe=None, milestones=None, name=None, quiet=True)
made = {}
try:
    made["_new_job"] = cc._new_job(_args, cmd="echo hi", log="/tmp/x.log",
                                   exit_file="/tmp/x.log.exit", pid=1)
except Exception as ex:
    made["_new_job"] = "RAISED %r" % (ex,)
for kind in ("slurm", "lsf", "pbs"):
    try:
        with cc.state_rw() as st:
            st["jobs"] = {}
        made[kind] = cc.attach_batch_job(kind, "4242", "/tmp", eta=None,
                                         name="probe-" + kind)
    except Exception as ex:
        made[kind] = "RAISED %r" % (ex,)
for how, got in sorted(made.items()):
    ck("a job can be made the %s way" % how, not str(got).startswith("RAISED"), str(got)[:80])
ck("a scheduler job records no exit file of its own",
   not (cc.state_ro()["jobs"].get("probe-pbs") or {}).get("exit_file"))
ck("but a locally run one does",
   isinstance(made.get("_new_job"), dict) and made["_new_job"].get("exit_file"))
with cc.state_rw() as st:
    st["jobs"] = {}

print()
print("=== numbers a bar cannot be drawn from ===")
# float() accepts "nan" and "inf", and a diverged training run prints the word
# on every line. One reaching the samples gave the bar a rate of nan, which it
# then showed to the user as "nans/it" while claiming no time remained.
import math as _math

ck("nan is not a number here", cc._float("nan") is None)
ck("nor is inf", cc._float("inf") is None and cc._float("-inf") is None)
ck("but a real one still is", cc._float("3.5") == 3.5)
_j = {"samples": []}
cc.record_sample(_j, float("nan"), time.time())
cc.record_sample(_j, float("inf"), time.time())
ck("a sample that is not a number is refused", _j["samples"] == [])
cc.record_sample(_j, 5, time.time())
ck("and a real one is kept", len(_j["samples"]) == 1)

_cfg = dict(cc.load_config())
_now = time.time()
_hostile = {
    "step beyond total": dict(step=150, total=100),
    "negative step": dict(step=-5, total=100),
    "zero total": dict(step=5, total=0),
    "percent over 100": dict(pct=180.0),
    "negative percent": dict(pct=-20.0),
    "nan percent": dict(pct=float("nan")),
    "infinite percent": dict(pct=float("inf")),
    "started in the future": dict(started=_now + 3600),
    "ended before it started": dict(state="done", ended=_now - 600, started=_now),
    "nan estimate": dict(est_total_s=float("nan")),
    "infinite estimate": dict(est_total_s=float("inf")),
    "negative estimate": dict(est_total_s=-500),
    "samples out of order": dict(samples=[[_now, 10], [_now - 30, 40]], step=10, total=100),
    "a nan among the samples": dict(samples=[[_now - 30, float("nan")], [_now, 5]],
                                    step=5, total=100),
    "malformed sample rows": dict(samples=[["x", "y"], None, [_now, 5]], step=5, total=100),
    "identical timestamps": dict(samples=[[_now, 1], [_now, 2]], step=2, total=100),
    "progress going backwards": dict(samples=[[_now - 60, 90], [_now, 10]], step=10, total=100),
}
_bad = []
for _name, _extra in sorted(_hostile.items()):
    _job = dict(state="running", started=_now - 60, samples=[], unit="it", updated=_now)
    _job.update(_extra)
    try:
        _e = cc.estimate(_job, _now, _cfg)
        _line = re.sub(r"\033\[[0-9;]*m", "", cc.render_line(_job, _cfg, width=100))
        _pct = _e.get("pct")
        _sane = _pct is None or (isinstance(_pct, (int, float)) and _math.isfinite(_pct)
                                 and -0.01 <= _pct <= 100.01)
        if not (_sane and "nan" not in _line.lower() and "inf" not in _line.lower()):
            _bad.append((_name, _pct, _line[:50]))
    except Exception as _ex:
        _bad.append((_name, "RAISED", repr(_ex)))
ck("every hostile job still draws a sane bar", not _bad, str(_bad[:2]))
for _key in ("max_jobs", "bar_width"):
    try:
        cc.coerce(_key, "nan")
        _rejected = False
    except ValueError:
        _rejected = True
    ck("a setting cannot be set to nan (%s)" % _key, _rejected)

print()
print("=== a job buried in a compound command ===")
# Claude writes the setup and the work in one call constantly - a heredoc that
# writes a script and then the line that runs it, or `mkdir -p out && python
# train.py`. Judging the whole thing by its first word called it trivial and
# left the training run untracked, which is the main case the plugin exists for.
_cases = [
    ("setup and then the job", "mkdir -p out && python3 train.py --epochs 50", True),
    ("a heredoc, then the job",
     "cat > t.py <<'X'\nimport time\nX\npython3 train.py", True),
    ("five thousand lines, then the job",
     "\n".join("mkdir -p d%d" % i for i in range(5000)) + "\npython3 train.py", True),
    ("a huge heredoc, then the job",
     "cat > f <<'X'\n" + ("data line\n" * 20000) + "X\npython3 train.py", True),
    ("trivial all the way through", "mkdir -p out && ls -la && echo done", False),
    ("just a listing", "ls -la", False),
    ("asking for help", "python train.py --help", False),
    ("a long token and a job", "python train.py " + "a" * 200000, True),
    ("thousands of echoes", " && ".join("echo %d" % i for i in range(5000)), False),
]
_wrong, _slow = [], []
for _name, _cmd, _want in _cases:
    _t0 = time.time()
    _got = cc.classify_command(_cmd)["track"]
    _took = time.time() - _t0
    if _got != _want:
        _wrong.append((_name, _got, _want))
    if _took > 1.0:
        _slow.append((_name, round(_took, 1)))
ck("a job is found wherever it sits in a compound command", not _wrong, str(_wrong))
ck("and deciding stays fast on pathological commands", not _slow, str(_slow))
# The self-reference guard is anchored, so it moved into the per-segment bucket
# when compound commands started being read part by part. It has to keep
# holding: the plugin wrapping its own invocation is how a wrapper recurses.
for _own in ("agent-progress run --name t --eta 1h -- python train.py",
             "/usr/local/bin/agent-progress exec --name train --after 20 --shell 'python train.py'",
             "agent-progress ls",
             "agent-progress update train --eta 2h",
             "python3 /somewhere/scripts/agent_progress.py run -- python train.py"):
    ck("the plugin does not wrap itself: %s" % _own[:40],
       not cc.classify_command(_own)["track"], _own[:60])

# A heredoc is how a script gets written; collapsing one onto a line put the
# script's own source into the middle of what claimed to be the command.
_with_heredoc = ("mkdir -p out && cat > train.py <<'PY'\nimport time\n"
                 "for i in range(14):\n    print(i)\nPY\npython3 train.py --epochs 13")
_shown = cc.command_for_display(_with_heredoc)
ck("a shown command does not include the script it wrote",
   "import time" not in _shown and "range(14)" not in _shown, _shown[:70])
ck("but it still shows what was actually run",
   "python3 train.py --epochs 13" in _shown and "mkdir -p out" in _shown, _shown[:70])
ck("and a command with no heredoc is untouched",
   cc.command_for_display("python3 train.py") == "python3 train.py")

ck("heredoc bodies are not read as commands",
   not cc.classify_command("cat > f <<'X'\npython3 train.py\nX\nls")["track"],
   "the text of a script is data, not a command being run")

print()
print("=== asking some other scheduler how a job is doing ===")
# The slurm path has its own suite; this is the generic one, used for anything
# with a --state-probe: LSF, PBS, a queue somebody wrote themselves. Coverage
# said none of it had ever run.
# Only "done" and "failed" are endings; everything else means "leave it alone",
# which the probe spells "running" and an unusable answer spells None. The two
# behave identically downstream - a job is never finished on a guess.
for _out, _want in (("RUNNING", "running"), ("COMPLETED", "done"), ("COMPLETE", "done"),
                    ("FAILED", "failed"), ("OUT_OF_MEMORY", "failed"),
                    ("TIMEOUT", "failed"), ("CANCELLED", "failed"),
                    ("CANCELLED_BY_12345", "failed"),
                    ("PENDING", "running"), ("", None), ("what?", "running"),
                    ("0", "done"), ("3", "failed")):
    _job = {"state_probe": "printf '%s'" % _out}
    ck("a probe saying %-14r reads as %s" % (_out, _want),
       cc.read_state_probe(_job) == _want, repr(cc.read_state_probe(_job)))
ck("a probe with nothing to run answers nothing", cc.read_state_probe({}) is None)
ck("a probe that fails answers nothing",
   cc.read_state_probe({"state_probe": "exit 7"}) is None)
ck("a probe that prints several lines uses the last",
   cc.read_state_probe({"state_probe": "printf 'noise\\nCOMPLETED'"}) == "done")
ck("and one that hangs does not hang the caller",
   cc.read_state_probe({"state_probe": "sleep 45"}) is None)

print()
print("=== folding a reading into a job ===")
_j = {"unit": "it", "samples": []}
cc.apply_reading(_j, {"step": 1, "total": 10}, time.time())
ck("a first step of 1 is taken as one done", _j.get("units") in (0.0, 1.0), str(_j.get("units")))
_z = {"unit": "it", "samples": []}
cc.apply_reading(_z, {"step": 0, "total": 10}, time.time())
ck("a job that starts counting at zero is noticed", _z.get("zero_indexed") is True,
   str(_z.get("zero_indexed")))
cc.apply_reading(_z, {"step": 4, "total": 10}, time.time())
ck("and then four means four", _z.get("units") == 4.0, str(_z.get("units")))
# a reading's pct is a fraction, not a number out of a hundred: the parser
# divides by 100 before it gets here
ck("the parser gives a percentage as a fraction",
   cc.parse_progress("Progress: 40%")["pct"] == 0.4,
   str(cc.parse_progress("Progress: 40%")["pct"]))
_p = {"unit": "it", "samples": [], "total": 50}
cc.apply_reading(_p, {"pct": 0.4}, time.time())
ck("a percentage against a known total becomes units", _p.get("units") == 20.0,
   str(_p.get("units")))
_q = {"unit": "it", "samples": []}
cc.apply_reading(_q, {"pct": 0.4}, time.time())
ck("and with no total it is kept as a percentage", _q.get("pct") == 0.4, str(_q.get("pct")))
ck("a reading with nothing in it changes nothing",
   cc.apply_reading({"unit": "it", "samples": []}, {}, time.time()) is False)

print()
print("=== the monitor a job gets from its flags ===")
import types as _t


def _mon(**kw):
    base = dict(monitor=None, pattern=None, log=None, milestone=None, milestones=None,
                glob=None, path=None, target_size=None, probe=None, state_probe=None)
    base.update(kw)
    return cc.build_monitor(_t.SimpleNamespace(**base))


ck("milestones given one at a time", _mon(milestone=["a", "b"])["kind"] == "milestones")
ck("and their order is kept", _mon(milestone=["a", "b"])["milestones"] == ["a", "b"])
ck("milestones given as one string", _mon(milestones="a;b;c")["milestones"] == ["a", "b", "c"])
ck("a glob makes a files monitor", _mon(glob="out/*.pt")["kind"] == "files")
ck("a path makes a size monitor", _mon(path="/tmp/x")["kind"] == "size")
ck("a target size is understood", _mon(path="/tmp/x", target_size="2GB")["target_bytes"]
   == 2 * 1000 ** 3 or _mon(path="/tmp/x", target_size="2GB")["target_bytes"] == 2 * 1024 ** 3,
   str(_mon(path="/tmp/x", target_size="2GB")["target_bytes"]))
ck("a probe command makes a probe monitor", _mon(probe="echo 1/2")["kind"] == "probe")
# no flags at all means no spec: the job works out how to watch itself
ck("and nothing at all leaves it to work it out", _mon() is None, str(_mon()))
ck("while asking for auto says so explicitly", _mon(monitor="auto") == {"kind": "auto"},
   str(_mon(monitor="auto")))

print()
print("=== a duration is the whole string, with real units ===")
for text, want in (("90", 90), ("90s", 90), ("5m", 300), ("2h30m", 9000), ("2d", 172800),
                   ("1w", 7 * 86400), ("1:30:00", 5400), ("2 hours", 7200), ("45 min", 2700),
                   ("1.5h", 5400), ("500ms", 0.5)):
    got = cc.parse_duration(text)
    ck("%-8r -> %s" % (text, want), abs(got - want) < 1e-6, str(got))
for text in ("-5m", "1e3", "2x", "1:x", "::", "nan:0", "inf:0", "99999999999999999999h", "5m junk", "1y"):
    try:
        cc.parse_duration(text)
        ck("%r is refused" % text, False, "accepted")
    except SystemExit:
        ck("%r is refused" % text, True)
ck("an empty duration is None, not an error", cc.parse_duration("  ") is None)

print()
print("=== sizes, percents, huge numbers ===")
for bad in ("1.2.3GB", ".", "GB", "1..5"):
    try:
        cc.parse_size(bad)
        ck("size %r is refused cleanly" % bad, False, "accepted")
    except SystemExit:
        ck("size %r is refused cleanly" % bad, True)
    except ValueError as ex:
        ck("size %r is refused cleanly" % bad, False, "traceback: %s" % ex)
got = cc.parse_progress("GPU util 1234%")
ck("'GPU util 1234%' is not 34%% done", got is None or got.get("pct") is None, str(got))
ck("'done 45%' still reads", (cc.parse_progress("done 45%") or {}).get("pct") == 0.45)
job = {"state": "running", "units": None, "total": None, "samples": []}
ck("a 320-digit step is ignored rather than raised",
   cc.apply_reading(job, {"step": int("1" * 320), "total": int("2" * 320)}, time.time()) is False
   and job.get("units") is None, str(job.get("units")))
arr = {"state": "running", "unit": "task", "total": 8, "total_locked": True, "units": 7.0, "samples": []}
ck("an array's task count is not overwritten by one task's log",
   cc.apply_reading(arr, {"step": 3, "total": 50}, time.time()) is False and arr["units"] == 7.0,
   str(arr["units"]))
st = {"jobs": {"a": {"id": "a", "state": "running", "eta_end": float("inf"), "total": 10 ** 20,
                     "pct": float("nan"), "started": time.time()}}}
cc._sanitize(st)
ck("inf, nan and a number too big to be a count are dropped on read",
   all(st["jobs"]["a"].get(k) is None for k in ("eta_end", "total", "pct")), str(st["jobs"]["a"]))
ck("a timestamp beyond the platform's clock renders as ?", cc.fmt_clock(1e20) == "?")
ck("an infinite duration renders as ?", cc.fmt_short(float("inf")) == "?")
ck("a pid of 0 or -1 is never alive", not cc.alive(0) and not cc.alive(-1))

print()
print("=== scheduler words ===")
ck("LSF EXIT is a failure", "EXIT" in cc.FAILED_STATES)
ck("PBS asks for the exit status once the job is finished",
   "Exit_status" in cc.PBS_STATE_CMD and "job_state" in cc.PBS_STATE_CMD)

print()
print("=== what stays outside the wrapper ===")
cfg = cc.load_config()
def verdict(c):
    return cc.classify_command(c, {}, cfg)
v = verdict("cd /tmp && python train.py")
ck("a leading cd stays in the caller's shell", v["track"] and v.get("prefix") == "cd /tmp && "
   and v.get("body") == "python train.py", str(v))
v = verdict("export X=1 && make check")
ck("so does a leading export", v["track"] and v.get("prefix") == "export X=1 && ", str(v))
ck("set -e is fine inside the wrapper", verdict("set -euo pipefail && python train.py")["track"])
ck("a cd after the work leaves the command alone", not verdict("python train.py && cd out")["track"])
ck("a quoted cd is not split", not verdict("cd 'a b' && python train.py")["track"])
ck("a self-backgrounded command is left alone", not verdict("python train.py &")["track"])
ck("nohup is left alone", not verdict("nohup python train.py &")["track"])
ck("AGENT_PROGRESS_NO_AUTO=1 in front of the command works",
   not verdict("AGENT_PROGRESS_NO_AUTO=1 python train.py")["track"])
ck("an inline assignment before the command is fine", verdict("FOO=1 python train.py")["track"])
ck("agent-progress after a cd is not re-wrapped",
   not verdict("cd repo && agent-progress run -- python train.py")["track"])
ck("the name comes from the work, not the cd", verdict("cd /tmp && python train.py")["name"] == "train")
ck("clip adds no escape code to a plain line", "\033" not in cc.clip("a" * 50, 20))
ck("but keeps colour balanced when there was some", cc.clip("\033[31m" + "a" * 50, 20).endswith("\033[0m"))

print()
print("=== the prefix split knows its limits ===")
cfg = load_cfg = cc.load_config()
def verdict(c):
    return cc.classify_command(c, {}, cfg)
ck("a continuation line inside a cd is not cut", not verdict("cd /tmp \\\n  && python train.py")["track"])
ck("source is never split from the work: the command is left whole",
   not verdict("source lib.sh && python train.py")["track"] and not verdict(". lib.sh; python train.py")["track"])
v = verdict("python train.py \\\n  model=resnet \\\n  data=cifar")
ck("hydra overrides on continuation lines are still tracked", v["track"], v["why"])
ck("make VAR=x on a continuation line is still tracked", verdict("make \\\n  CC=gcc")["track"])
ck("an & inside a trailing comment is not backgrounding", verdict("pytest # trailing &")["track"]
   and verdict("pytest; # &")["track"])
ck("a real trailing & still is", not verdict("pytest &")["track"] and not verdict("pytest 2>&1 &")["track"])
ck("a && b is not backgrounding", verdict("make && python train.py")["track"])
ck("./train.sh is named train", cc.suggest_job_name("./train.sh") == "train", cc.suggest_job_name("./train.sh"))
ck("./scripts/run_eval.sh --x is named run_eval", cc.suggest_job_name("./scripts/run_eval.sh --x") == "run_eval",
   cc.suggest_job_name("./scripts/run_eval.sh --x"))
ck("bash train.sh is named train", cc.suggest_job_name("bash train.sh") == "train", cc.suggest_job_name("bash train.sh"))
for text, want in (("10 msec", 0.01), ("5 secs", 5), ("3 hrs", 10800), ("2 days", 172800), ("1 hour 5 minutes", 3900)):
    ck("%r -> %s" % (text, want), abs(cc.parse_duration(text) - want) < 1e-9, str(cc.parse_duration(text)))
for text in ("1 month", "3 hz", "2 dozen", "1 mile"):
    try:
        cc.parse_duration(text)
        ck("%r is refused" % text, False, "accepted")
    except SystemExit:
        ck("%r is refused" % text, True)

print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)
