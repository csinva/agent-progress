#!/usr/bin/env python3
"""A narrated tour of agent-tqdm.

Launches one short job per monitor kind - a log counter, output files, a
growing file, named stages, a shell probe - plus one that crashes and one that
is too short to be worth showing. Then it narrates what to look for while they
run, in the terminal *and* in the Claude Code statusline.

Everything it creates is namespaced `demo-` and removed at the end.

    python3 demo/tour.py            # or:  agent-tqdm demo --tour
    python3 demo/tour.py --speed 2  # twice as fast
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(os.path.dirname(HERE), "scripts", "agent_tqdm.py")

DIM, BOLD, CYAN, YELLOW, GREEN, RESET = (
    "\033[38;5;244m", "\033[1m", "\033[38;5;44m", "\033[38;5;179m",
    "\033[38;5;42m", "\033[0m")


def cli(*args, **kw):
    """Drive the real CLI, exactly as a user would."""
    return subprocess.run([sys.executable, ENGINE] + list(args),
                          capture_output=True, text=True, **kw).stdout


# Each entry is one job, chosen to demonstrate one thing. `script` is written
# into a scratch directory and run for real; nothing here is simulated output.
WORKLOADS = [
    {
        "name": "demo-train",
        "shows": "a log counter, and an ETA that corrects a bad guess",
        "eta": "25s",                      # deliberately wrong: it takes ~55s
        "flags": ["--unit", "ep"],
        "script": """
import time, sys
N = 24
for i in range(1, N + 1):
    time.sleep({tick} * 1.8)
    print("Epoch %d/%d  loss %.3f" % (i, N, 2.4 / (i ** 0.5)), flush=True)
""",
    },
    {
        "name": "demo-shards",
        "shows": "counting output files as they appear",
        "eta": "50s",
        "flags": ["--glob", "{scratch}/out/shard-*.parquet", "--total", "12",
                  "--unit", "file"],
        "script": """
import os, time
os.makedirs("{scratch}/out", exist_ok=True)
for i in range(12):
    time.sleep({tick} * 3.4)
    open("{scratch}/out/shard-%02d.parquet" % i, "w").write("x")
""",
    },
    {
        "name": "demo-index",
        "shows": "a file growing toward a target size",
        "eta": "50s",
        "flags": ["--path", "{scratch}/index.bin", "--target-size", "8MB"],
        "script": """
import time
with open("{scratch}/index.bin", "wb") as f:
    for i in range(16):
        time.sleep({tick} * 2.5)
        f.write(b"0" * 524288); f.flush()
""",
    },
    {
        "name": "demo-etl",
        "shows": "named stages, for output with no numbers at all",
        "eta": "50s",
        "flags": ["--milestones",
                  "reading source;normalizing;joining;writing;verifying"],
        "script": """
import time
for s in ["reading source", "normalizing", "joining", "writing", "verifying"]:
    print(s, flush=True)
    time.sleep({tick} * 8.5)
""",
    },
    {
        "name": "demo-queue",
        "shows": "a shell probe, for state that lives outside the job",
        "eta": "50s",
        "flags": ["--probe", "wc -l < {scratch}/rows.txt", "--total", "40",
                  "--unit", "row"],
        "script": """
import time
for i in range(40):
    time.sleep({tick} * 1.2)
    open("{scratch}/rows.txt", "a").write("row\\n")
""",
    },
    {
        "name": "demo-flaky",
        "shows": "a crash: skull, decoded exit, report to Claude",
        "eta": "50s",
        "flags": [],
        "script": """
import time
print("connecting to feature store", flush=True)
time.sleep({tick} * 5)
print("loading batch 1", flush=True)
time.sleep({tick} * 5)
raise MemoryError("unable to allocate 8.00 GiB for embedding table")
""",
    },
    {
        "name": "demo-quickie",
        "shows": "too short to show: tracked, but kept off the statusline",
        "eta": "20s",
        "flags": [],
        "script": "import time\ntime.sleep({tick} * 14)\n",
    },
]

# (seconds elapsed, what to point out)
NARRATION = [
    (0, "Seven jobs launched. Look at your Claude Code statusline: the top 3 are "
        "there, live."),
    (7, "Every bar is driven by a different signal - a log counter, files on disk, "
        "a file's size, stage names, a shell command."),
    (14, "demo-quickie appears here because `agent-tqdm ls` shows everything - but "
         "check your statusline, it is absent there. Under the 2-minute floor."),
    (22, "demo-train was given a deliberately wrong 25s estimate. It is measuring "
         "its own throughput now - watch the ETA move."),
    (30, "A yellow `est ...` on a bar means that job's total estimate has been "
         "revised away from the original guess."),
    (38, "demo-flaky has crashed by now: skull, and the exit decoded rather than "
         "left as a number."),
    (48, "demo-etl has no numbers in its output at all - progress is the stages it "
         "has printed."),
]


def render(elapsed, note, live):
    bars = cli("ls").rstrip()
    bars = "\n".join("  " + ln for ln in bars.splitlines()
                     if "demo-" in ln or not ln.strip())
    if live:
        sys.stdout.write("\033[H\033[J")
    header = "%sagent-tqdm tour%s  %s+%02ds%s" % (BOLD, RESET, DIM, elapsed, RESET)
    print("\n" + header + "\n")
    print(bars if bars.strip() else "  (starting...)")
    if note:
        print("\n  %s->%s %s" % (YELLOW, RESET, note))
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser(description="A narrated tour of agent-tqdm.")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="run the tour faster (2 = twice as fast)")
    ap.add_argument("--no-cleanup", action="store_true",
                    help="leave the demo jobs in place afterwards")
    args = ap.parse_args()

    tick = 1.0 / max(0.2, args.speed)
    live = sys.stdout.isatty()
    scratch = tempfile.mkdtemp(prefix="agent-tqdm-tour-")

    print("\n%sagent-tqdm%s - a live tour\n" % (BOLD, RESET))
    print("  Seven real jobs are about to run. Nothing here is faked: each one")
    print("  writes real output and is watched exactly as your own jobs would be.")
    print("\n  %sWatch your Claude Code statusline while this runs.%s\n" % (CYAN, RESET))
    print("  %sOne difference: the tour polls every second so you can see movement."
          % DIM)
    print("  Real jobs are observed every 2 minutes, or once per 5% of their")
    print("  estimated length - whichever is less often.%s\n" % RESET)
    time.sleep(2.5 * tick)

    for w in WORKLOADS:
        path = os.path.join(scratch, w["name"] + ".py")
        with open(path, "w") as f:
            f.write(w["script"].format(scratch=scratch, tick=tick))
        flags = [a.format(scratch=scratch) for a in w["flags"]]
        cli("run", "--name", w["name"], "--eta", w["eta"], "--interval", "1s",
            "--desc", w["shows"], *(flags + ["--", sys.executable, path]))
        print("  %s+%s %-14s %s%s%s" % (GREEN, RESET, w["name"], DIM, w["shows"], RESET))
    print()
    time.sleep(2 * tick)

    start = time.time()
    shown = set()
    try:
        while True:
            elapsed = int(time.time() - start)
            note = None
            for at, text in NARRATION:
                if elapsed >= at and at not in shown:
                    shown.add(at)
                    note = text
            if live or note or elapsed % 8 == 0:
                render(elapsed, note, live)
            # only the tour's own jobs decide when the tour is over
            try:
                jobs = json.loads(cli("ls", "--json") or "[]")
            except ValueError:
                jobs = []
            running = sum(1 for j in jobs if (j.get("id") or "").startswith("demo-")
                          and j.get("state") == "running")
            if running == 0 and elapsed > 10:
                break
            if elapsed > 200:
                break
            time.sleep(1.2 if live else 2.0)
    except KeyboardInterrupt:
        print("\n  interrupted")

    print("\n%sFinal state%s" % (BOLD, RESET))
    print("\n".join("  " + ln for ln in cli("ls").rstrip().splitlines()
                    if "demo-" in ln))

    print("\n%sYour statusline right now%s %s(same jobs, filtered: at most %s rows, "
          "and nothing under the length floor)%s\n" % (BOLD, RESET, DIM, "3", RESET))
    sl = subprocess.run([sys.executable, ENGINE, "statusline"], input="{}",
                        capture_output=True, text=True).stdout.rstrip()
    print("\n".join("  " + ln for ln in sl.splitlines()))

    report = cli("inbox", "--drain").strip()
    if report and "no undelivered" not in report:
        print("\n%sThe crash report Claude receives%s %s(delivered by the Stop hook, "
              "the moment Claude finishes its next turn)%s\n"
              % (BOLD, RESET, DIM, RESET))
        print("\n".join("  " + ln for ln in report.splitlines()))

    print("\n%sWhat you just saw%s" % (BOLD, RESET))
    for line in [
        "a log counter, output files, a file's size, named stages and a shell",
        "  probe - five different ways of knowing how far along a job is",
        "an ETA that started as a guess and corrected itself from measurement",
        "a crash that reported itself, with the traceback attached",
        "a short job deliberately kept off the statusline",
    ]:
        print("  %s-%s %s" % (CYAN, RESET, line))
    print("\n  %sagent-tqdm config%s to change any of it, "
          "%sagent-tqdm preview%s to see the result.\n" % (CYAN, RESET, CYAN, RESET))

    if not args.no_cleanup:
        for w in WORKLOADS:
            cli("rm", w["name"])
        shutil.rmtree(scratch, ignore_errors=True)
        print("  %s(demo jobs removed; your real jobs were untouched)%s\n" % (DIM, RESET))
    else:
        print("  %s(left in place: %s)%s\n" % (DIM, scratch, RESET))
    return 0


if __name__ == "__main__":
    sys.exit(main())
