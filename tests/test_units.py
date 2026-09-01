#!/usr/bin/env python3
"""Unit tests for agent-progress's internals.

run_tests.py checks that the CLI behaves; this checks the parts underneath it -
duration and size parsing, progress scraping, the estimator, line clipping, the
monitors, and config coercion. These are where a wrong answer is quiet: a bar
that is subtly wrong looks like a bar.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import time

ENGINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "agent_progress.py")
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


run("rm", "--all")
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
run("rm", "--all")

print()
print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)
