---
name: agent-tqdm
description: Track any long-running job with a tqdm-style progress bar in the Claude Code statusline - training runs, data pipelines, test suites, builds, migrations, downloads, simulations, backups, sweeps. Use when the user starts or asks about something that will take more than a couple of minutes, says "how long will this take", "how far along is it", "track this", "run this in the background", "show me a progress bar", or wants an ETA on something already running. Also use to set up monitoring for a job with no obvious progress output.
---

# Progress bars for long-running jobs

`agent-tqdm` puts a live bar in the user's statusline for any job that takes
minutes to days. Your involvement is almost entirely **up front**:

1. Decide **how this particular job's progress can be observed**.
2. Give a rough estimate of how long it will take.
3. Launch it.

After that a background watcher does the work. It re-observes the job on a slow
cadence, recomputes the total estimate from what it sees, and marks the job done
or failed with the real exit code. **Do not poll the job yourself** — no loops,
no repeated `ls`, no waiting. Check in only when you have a reason to.

Use `agent-tqdm`. If it is not on PATH, use
`python3 ~/.claude/skills/agent-tqdm/scripts/agent_tqdm.py`.

`agent-tqdm autotrack '<command>'` shows whether a given command would be caught
automatically, and why.

## It costs nothing until a job proves slow

Long-running commands are tracked automatically, but **only after they have
earned it**. A command you run through Bash is wrapped so that it runs exactly
as it would have; if it finishes within 20 seconds nothing happens at all - no
job, no bar, no message, no tokens. You will not even know it was wrapped.

Only when a command is *still running* after that threshold is it tracked. At
that point it is handed off: it keeps running in the background, and the tool
result ends with a note telling you so.

When you get that note:

- **The command is still running.** Do not re-run it. Its remaining output goes
  to the job's log: `agent-tqdm log <id> -n 40`, and `agent-tqdm ls --json` for
  progress and state.
- **Give it an estimate if it is worth one.** The hook cannot guess a duration -
  that is the one thing only you can supply:

  ```bash
  agent-tqdm update <id> --eta 40m --note "what it is doing"
  ```

  Judge whether it is worth doing. A job that will be over in a minute never
  reaches the statusline anyway, so leave it alone and carry on. Spend the
  estimate on things that will actually run for a while.
- **Check what it is watching.** The handoff picks no monitor, so progress comes
  from reading markers in the log. If the job instead writes files, grows one
  file, or prints stage names, say so:

  ```bash
  agent-tqdm update <id> --monitor files --glob 'out/*.parquet' --total 500
  ```

You can still start a job yourself with `agent-tqdm run`, and it is better when
you already know something will be slow: the bar is then correct from the first
frame instead of starting blind 20 seconds in.

Under the `instruct` setting the command is stopped before it starts and you are
asked to relaunch it with an estimate and monitor chosen up front. Follow that
message if you see it - same two decisions, made earlier.

## Step 1: how will progress be observed?

This is the part that needs your judgement, and it is the whole reason a model
sets this up rather than a config file. Most jobs do not print `Epoch 3/50`.
Look at what the job actually *does*, and pick the signal that moves:

| the job… | use | flags |
| --- | --- | --- |
| prints a counter or a tqdm bar | `auto` (default) | *nothing* |
| prints a counter in an odd format | `log` | `--pattern 'done (?P<step>\d+) of (?P<total>\d+)'` |
| prints named stages, no numbers | `milestones` | `--milestones 'loading;training;evaluating;saving'` |
| writes output files one by one | `files` | `--glob 'out/shard-*.parquet' --total 500` |
| grows one output file or directory | `size` | `--path out/index.bin --target-size 12GB` |
| has state queryable from outside | `probe` | `--probe 'psql -tAc "select count(*) from rows"' --total 2000000` |
| exposes nothing at all | `time` | `--monitor time` |

`agent-tqdm monitors` prints this list with examples.

How to choose: read the script or command, and skim any output it has already
produced (`head`, or `--help`). Ask what changes on disk or in the log as it
progresses. Prefer a signal that is **monotonic and bounded** — a count with a
known total beats a percentage, which beats named stages, which beats nothing.
`milestones` is the workhorse for scripts that just narrate what they are
doing; `probe` is the universal escape hatch when the state lives somewhere
else entirely (a database, a queue, an API, a remote host).

If nothing is observable, that is a fine answer — use `--monitor time` and the
bar runs on your estimate alone. That is still better than no bar.

## Step 2: estimate the duration

`--eta` is time **remaining from now**. It only has to be roughly right; the
watcher corrects it from observed throughput as the job runs.

Look for evidence cheaply, and stop as soon as you have an order of magnitude:

- Has this run before? Old logs, output directories, checkpoint or artifact
  mtimes. A previous run is the best possible evidence.
- What is the work? Item count times per-item cost — rows, files, epochs,
  tests, images, GB to transfer.
- What is the machine? `nvidia-smi` for GPU work, core count for parallel work,
  link speed for transfers.
- Otherwise, reason by analogy and commit to an order of magnitude.

Say the estimate and its one-line basis to the user. If you genuinely have no
basis, **omit `--eta`** — the bar renders as an indeterminate sweep instead of
inventing a number. Do not fabricate confidence.

**If you expect it to finish in under about two minutes, do not track it at
all** — just run it normally and report the result. Short jobs are deliberately
hidden from the statusline (a bar that flashes past is noise), so tracking one
adds nothing. If the user explicitly wants a short job pinned anyway, pass
`--force-show`. A job you estimated as short but which is still running past the
threshold appears on its own, so an underestimate is not a problem.

## Step 3: launch

```bash
agent-tqdm run --name ingest --eta 40m --glob 'out/*.parquet' --total 500 --unit file \
  -- python pipeline.py --input raw/
```

`run` detaches the process, captures stdout and stderr, and tracks the pid.
Everything after `--` is the command. Prefer this over the Bash tool's own
background mode when the user wants to see progress — only `run` makes a bar.

For something already running:

```bash
agent-tqdm start reindex --pid 45123 --log /var/log/reindex.log --eta 3h
```

Then tell the user the expected wall-clock finish time and stop. The job is
detached; the statusline and the completion notification are how they follow it.

## After launch: leave it alone

Updates happen on their own, at most once every 2 minutes and at most once per
5% of the estimated total — so a 10-hour job is re-observed every 30 minutes,
not every second. The total estimate is recomputed at each observation, and the
cadence stretches with it. The bar shows `est 2h15m (+45m)` when the estimate has
moved materially from your first guess.

You are told about running jobs at session start and on each user prompt, so you
can volunteer status without checking anything.

**Intervene only when you know something the watcher cannot see:**

```bash
agent-tqdm ls --json                      # status of everything, structured
agent-tqdm log ingest -n 40               # read recent output
agent-tqdm update ingest --eta 90m --note "hit the slow shard set"
agent-tqdm update ingest --monitor files --glob 'out/**/*.parquet'   # fix the monitor
```

Reasons to intervene: the log shows an error, a stall, or a retry loop; the job
has phases with very different costs and it just changed phase (add
`--reset-rate` so the old throughput is discarded); the monitor you picked is
clearly not tracking anything (`agent-tqdm ls --json` shows no movement and
`monitor_kind` is not `time`); or the user asks.

Do not re-run `agent-tqdm ls` repeatedly in one turn, and never sleep-loop
waiting for a job. If the user wants genuinely periodic reporting, use the
`/loop` skill.

## When a job crashes

You will be handed a crash report unprompted — either as the reason your turn
was blocked, or as context on the next message. It looks like:

```
💀 A tracked job CRASHED while you were working: 'trainer'
  SIGKILL - killed outright - most often the OOM killer after 04:12
  command: python train.py --epochs 50
  log: ~/.claude/progress/logs/trainer.log
  last output: ...
```

When you get one, **tell the user immediately** — before continuing whatever you
were doing, and even if it is unrelated to the current task. Then:

1. Say plainly what died and how long it ran.
2. Read the cause out of the last output you were given. `SIGKILL` usually means
   the OOM killer; `SIGSEGV` a native crash; a plain non-zero exit means the
   traceback in the log tail is the real story. Read more with
   `agent-tqdm log <id> -n 60` if the tail is not enough.
3. Suggest a concrete fix if the cause is clear (smaller batch size, more
   memory, a missing file).
4. **Do not re-run the job without asking.** It may have burned hours, and
   re-running it blindly can repeat an expensive failure.

Each report is delivered once, so do not wait for a repeat — act on it when you
see it. A job the user cancelled deliberately is not a crash and is never
reported this way.

## Reporting status

`agent-tqdm ls --json` gives `percent`, `elapsed_human`, `remaining_human`,
`eta_clock`, `total_estimate_human`, `initial_estimate_human`, `eta_source`,
`monitor` and `next_update_in_s`.

Give the wall-clock finish time — "should land around 4:15pm" beats "63%".
When `eta_source` is `claude`, say it is still your estimate with no measured
throughput behind it yet. When `total_estimate_human` has drifted from
`initial_estimate_human`, say so — that is the useful news.

## Finishing

Jobs launched with `run` finish themselves and fire a desktop notification. Mark
state by hand only for jobs whose end the tool cannot see:

```bash
agent-tqdm done ingest
agent-tqdm fail ingest --exit-code 137 --note "OOM killed"
agent-tqdm cancel ingest        # also SIGTERMs the process
```

## Appearance

Everything about the bar is configurable — width, style, which fields appear,
colors, thresholds. If the user asks for it to look different, do not edit the
script: `agent-tqdm config` lists every setting with its default, `agent-tqdm
config --set key=value` changes one, and `agent-tqdm preview` renders sample
bars so they can see the result immediately. `agent-tqdm config --preset
minimal|rich|tqdm|plain|quiet` covers the common requests in one step.
