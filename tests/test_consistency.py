#!/usr/bin/env python3
"""Self-consistency: the tables the code reads from itself.

Settings, presets, patterns, styles, monitors and hooks are all declared in one
place and consumed in another. Nothing here exercises behaviour; it checks that
the declarations still agree with the code that reads them, which is the kind of
thing that rots quietly as a project changes.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "scripts", "agent_progress.py")
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
    if not cond:
        print("  FAIL %s   <- %s" % (name, detail))
        FAILS.append(name)


print("=== every declared setting is self-consistent ===")
for k, spc in cc.CONFIG_SPEC.items():
    d = spc["default"]
    try:
        rt = cc.coerce(k, d)
        ok = True
    except Exception as ex:
        ok, rt = False, str(ex)
    ck("default of %s survives its own validator" % k, ok, str(rt))
    ck("%s has help" % k, bool(spc["help"]) and not spc["help"][0].isupper(),
       spc["help"][:40])
    ck("%s is in a known group" % k,
       spc["group"] in {g for g, _t in cc.CONFIG_GROUPS}, spc["group"])

print()
print("=== every preset sets real keys to legal values ===")
for name, preset in cc.CONFIG_PRESETS.items():
    for k, v in preset.items():
        ck("preset %s: %s" % (name, k), k in cc.CONFIG_SPEC, "unknown key")
        if k in cc.CONFIG_SPEC:
            try:
                cc.coerce(k, v); okv = True; why = ""
            except Exception as ex:
                okv, why = False, str(ex)
            ck("preset %s: %s=%r legal" % (name, k, v), okv, why)

print()
print("=== every regex compiles ===")
for group, items in [("AUTO_TRACK_PATTERNS", [p for p, _l in cc.AUTO_TRACK_PATTERNS]),
                     ("AUTO_TRACK_IGNORE", cc.AUTO_TRACK_IGNORE),
                     ("_NAME_HINTS", cc._NAME_HINTS),
                     ("BUILTIN_PATTERNS", [p.pattern for _n, p in cc.BUILTIN_PATTERNS])]:
    okall, first = True, ""
    for rx in items:
        try:
            re.compile(rx)
        except re.error as ex:
            okall, first = False, "%s: %s" % (rx[:30], ex)
    ck("%s (%d) all compile" % (group, len(items)), okall, first)

print()
print("=== every bar style is well formed ===")
for name, tup in cc.STYLES.items():
    ck("style %s has 5 parts" % name, len(tup) == 5, str(tup))
    left, right, fill, partials, track = tup
    ck("style %s uses single cells" % name,
       all(len(x) <= 1 for x in (left, right, fill, track)), repr(tup))
ck("every style is offered by the config",
   set(cc.STYLES) == set(cc.CONFIG_SPEC["style"]["choices"]),
   "%s vs %s" % (sorted(cc.STYLES), sorted(cc.CONFIG_SPEC["style"]["choices"])))

print()
print("=== every monitor kind is implemented ===")
for kind in cc.MONITOR_KINDS:
    try:
        cc.monitor_reading({"monitor": {"kind": kind}, "log": None}, None)
        ok = True; why = ""
    except Exception as ex:
        ok, why = False, "%s: %s" % (type(ex).__name__, ex)
    ck("monitor %s runs" % kind, ok, why)
ck("MONITOR_HELP documents each kind",
   all(k in cc.MONITOR_HELP for k in cc.MONITOR_KINDS),
   [k for k in cc.MONITOR_KINDS if k not in cc.MONITOR_HELP])

print()
print("=== signal names ===")
for code, name in cc.SIGNAL_NAMES.items():
    ck("crash_reason knows %s" % name, cc.crash_reason(128 + code)[0] == name)
ck("every hint refers to a known signal",
   all(s in cc.SIGNAL_NAMES for s in cc.SIGNAL_HINTS),
   [s for s in cc.SIGNAL_HINTS if s not in cc.SIGNAL_NAMES])

print()
print("=== every subcommand has help and runs --help ===")
top = subprocess.run([sys.executable, ENGINE, "--help"], capture_output=True, text=True).stdout
cmds = [c for c in re.findall(r"^\s{4}(\w[\w-]*)", top, re.M) if not c.startswith("_")]
for c in sorted(set(cmds)):
    r = subprocess.run([sys.executable, ENGINE, c, "--help"], capture_output=True, text=True)
    ck("%s --help" % c, r.returncode == 0 and len(r.stdout) > 30, r.stderr[:50])

print()
print("=== plugin metadata ===")
man = json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
ck("manifest names the plugin", man.get("name") == "agent-progress")
ck("manifest has a version", bool(man.get("version")))
hooks = json.load(open(os.path.join(ROOT, "hooks", "hooks.json")))
for ev, entries in hooks["hooks"].items():
    for e in entries:
        for h in e["hooks"]:
            path = h["command"].split('"')[1]
            real = path.replace("${CLAUDE_PLUGIN_ROOT}", ROOT)
            ck("%s hook script exists" % ev, os.path.exists(real), real)
for f in ["skills/agent-progress/SKILL.md", "commands/track.md", "commands/progress.md"]:
    head = open(os.path.join(ROOT, f)).read()
    ck("%s has frontmatter" % f, head.startswith("---") and head.count("---") >= 2)


print()
README = open(os.path.join(ROOT, "README.md")).read()
for k in sorted(cc.CONFIG_SPEC):
    ck("the README mentions %s" % k, k in README)
for name in sorted(cc.CONFIG_PRESETS):
    ck("the README mentions preset %s" % name,
       "`%s`" % name in README or "--preset %s" % name in README)
for kind in cc.MONITOR_KINDS:
    ck("the README mentions the %s monitor" % kind, "`%s`" % kind in README)
m = re.search(r"one of (\d+) patterns", README)
ck("the README's pattern count is right",
   m and int(m.group(1)) == len(cc.AUTO_TRACK_PATTERNS),
   "says %s, there are %d" % (m.group(1) if m else "-", len(cc.AUTO_TRACK_PATTERNS)))

print()
# A crash example in the skill pointed at ~/.claude/progress/logs for a long
# time; the plugin has never written there. Paths in prose are read by people
# and repeated by Claude, so they have to be paths that exist.
_REAL = {os.path.basename(cc.ROOT), "skills", "settings.json", "plugins", "agent-progress"}
_docs = ["README.md", os.path.join("skills", "agent-progress", "SKILL.md")]
_docs += [os.path.join("commands", f) for f in sorted(os.listdir(os.path.join(ROOT, "commands")))
          if f.endswith(".md")]
for _doc in _docs:
    _text = open(os.path.join(ROOT, _doc)).read()
    _bad = sorted({m for m in re.findall(r"~/\.claude/([A-Za-z0-9_.-]+)", _text)
                   if m not in _REAL})
    ck("%s names only paths the plugin uses" % _doc, not _bad, str(_bad))

print()
# Four separate times a test has killed or counted processes with a pattern
# general enough to match another run's - the sandbox gives each run its own
# state directory, but there is only one process table. Any pkill or pgrep in
# the suites must name something unique to the run.
_offenders = []
for _name in sorted(os.listdir(os.path.join(ROOT, "tests"))):
    if not _name.endswith(".py"):
        continue
    _src = open(os.path.join(ROOT, "tests", _name)).read()
    for _m in re.finditer(r'"(pkill|pgrep)",\s*"-f",\s*([^\]]+)\]', _src):
        _pattern = _m.group(2).strip()
        # acceptable: built from TAG, or from a variable holding one
        if "TAG" in _pattern or "+ " in _pattern or _pattern.startswith(("_", "orphan", "jid")):
            continue
        _offenders.append("%s: %s %s" % (_name, _m.group(1), _pattern[:40]))
ck("no test kills or counts processes by a pattern another run could match",
   not _offenders, str(_offenders[:3]))

print()
# `_spec` defaults its type to "int", so a setting whose default is True but
# which forgets to say "bool" is declared an integer - and setting it to `false`
# then fails with a message about converting a string to a number, while the
# setting silently keeps its old value. announce_done shipped that way for
# exactly as long as it took to write a test for it.
_KIND = {bool: "bool", int: "int", float: "float", str: "str", type(None): "str"}
_mismatched = []
for _key in sorted(cc.CONFIG_SPEC):
    _spec_ = cc.CONFIG_SPEC[_key]
    _want = _KIND.get(type(_spec_["default"]))
    if _want == "int" and _spec_["type"] == "float":
        continue                     # a float setting may sensibly default to a whole number
    if _want != _spec_["type"]:
        _mismatched.append((_key, type(_spec_["default"]).__name__, _spec_["type"]))
ck("every setting's declared type matches its default", not _mismatched, str(_mismatched))

# and every setting can actually be set to something other than its default
_unsettable = []
for _key in sorted(cc.CONFIG_SPEC):
    _spec_ = cc.CONFIG_SPEC[_key]
    if _spec_["type"] == "bool":
        _try = "false" if _spec_["default"] else "true"
    elif _spec_["choices"]:
        _try = next((c for c in _spec_["choices"] if c != _spec_["default"]), None)
    elif _spec_["type"] in ("int", "float"):
        _try = str((_spec_["lo"] if _spec_["lo"] is not None else 0) + 1)
    else:
        _try = "x"
    if _try is None:
        continue
    try:
        _got = cc.coerce(_key, _try)
        if _got == _spec_["default"]:
            _unsettable.append((_key, "unchanged"))
    except Exception as _ex:
        _unsettable.append((_key, repr(_ex)[:40]))
ck("and every setting accepts a value other than its default",
   not _unsettable, str(_unsettable[:3]))

print()
# `--help` is the whole documentation for anyone not reading the README, and
# flags shared between subcommands had drifted: --desc was described under `run`
# and blank under `exec`, --total under `start` and blank under `update`.
import argparse as _argparse

_subs = cc.build_parser()._subparsers._group_actions[0].choices
_undocumented = []
for _name, _sp in sorted(_subs.items()):
    if _name.startswith("_"):
        continue
    for _a in _sp._actions:
        if _a.dest == "help" or _a.help is _argparse.SUPPRESS:
            continue
        if not _a.help:
            _undocumented.append("%s %s" % (_name, "/".join(_a.option_strings) or _a.dest))
ck("every option and argument says what it is for", not _undocumented, str(_undocumented[:4]))

# and a flag that appears on several subcommands should mean the same thing
_by_flag = {}
for _name, _sp in sorted(_subs.items()):
    if _name.startswith("_"):
        continue
    for _a in _sp._actions:
        for _opt in _a.option_strings:
            if _a.help and _a.help is not _argparse.SUPPRESS:
                _by_flag.setdefault(_opt, {}).setdefault(_a.help, []).append(_name)
# Flags that genuinely mean different things in different places. Anything not
# on this list that differs is drift, not intent.
_MAY_DIFFER = {
    "--eta": "a prior for a new job, a revision for a running one, a fixed value for the demo",
    "--interval": "probe cadence for a job, redraw rate for the dashboard",
    "--json": "the shape of whatever that subcommand prints",
    "--name": "what to call the job, worded for how the job arrives",
    "--note": "a status note on a bar, or the reason a job was ended",
    "--cwd": "where to run a command, but where a scheduler writes for a queued job",
    "--path": "the config file, or the file a size monitor measures",
    "--set": "a setting saved, or a setting only tried",
}
_conflicting = {f: sorted(h) for f, h in _by_flag.items()
                if len(h) > 1 and f not in _MAY_DIFFER}
ck("a flag on several subcommands is described the same way", not _conflicting,
   str(list(_conflicting)[:4]))
ck("and every flag allowed to differ actually does",
   all(len(_by_flag.get(f, {})) > 1 for f in _MAY_DIFFER if f in _by_flag),
   str([f for f in _MAY_DIFFER if f in _by_flag and len(_by_flag[f]) == 1]))

print("  %d declarations checked" % CHECKS[0])
print()
print("=== %d checks, %d failed ===" % (CHECKS[0], len(FAILS)))
for f in FAILS:
    print("   -", f)
sys.exit(1 if FAILS else 0)
