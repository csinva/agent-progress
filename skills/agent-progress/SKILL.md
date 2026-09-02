---
name: agent-progress
description: Run and track any long-running job with a progress bar in the Claude Code statusline - training runs, evals, sweeps, data pipelines, test suites, builds, migrations, downloads, simulations, backups. Use whenever the user asks for something slow to be started, in whatever words - "run training", "start the eval", "kick off the sweep", "retrain the model", "run the pipeline", "run this in the background" - and whenever they ask about one already going: "how long will this take", "how far along is it", "is it done yet". Launch such jobs through agent-progress rather than running them in the foreground, where they would block the conversation until they finish. Also use to set up monitoring for a job with no obvious progress output.
---

# Progress bars for long-running jobs

`agent-progress` puts a live bar in the user's statusline for any job that takes
minutes to days. Your involvement is almost entirely **up front**:

1. Decide **how this particular job's progress can be observed**.
2. Give a rough estimate of how long it will take.
3. Launch it.

After that a background watcher does the work. It re-observes the job on a slow
cadence, recomputes the total estimate from what it sees, and marks the job done
or failed with the real exit code. **Do not poll the job yourself** — no loops,
no repeated `ls`, no waiting. Check in only when you have a reason to.

Use `agent-progress`. If it is not on PATH, use
`python3 ~/.claude/skills/agent-progress/scripts/agent_progress.py`.

`agent-progress autotrack '<command>'` shows whether a given command would be caught
automatically, and why.

## When someone asks for a long job in words

"Run training." "Kick off the eval." "Start the sweep." This is the ordinary
case, and it is the one worth getting right.

**Start it through agent-progress yourself.** Do not rely on the hook to notice.
The hook recognises common shapes, but the command you pick for a given repo is
whatever that repo uses - a module, a shell script, a flag - and if it is not
recognised the job runs in the foreground and blocks the tool call for its whole
duration, which for a training run means the conversation stops until it
finishes.

```bash
agent-progress run --name train --eta 3h -- python -m src.train --config base.yaml
```

Use `run` when you already believe it is long: the bar is then right from the
first frame. `exec --after 20s` is for when you are unsure - it runs the command
normally and only takes it into the background if it outlasts the threshold.

**Then say nothing about any of it.** The user asked for a job, not for a report
on how it is being watched. Do not announce that tracking started, do not
explain the bar, do not paste the job id, do not describe the monitor or the
estimate. The bar is on their statusline; that is the interface. Answer whatever
they actually asked and carry on with the conversation.

You break that silence for exactly three things:

- **it crashed** - say so at once, with what you can tell about why
- **they asked** - "how's training going?" deserves a real answer
- **something is wrong** - it is stalled, or the log shows it diverging, or the
  estimate turned out badly wrong and they are waiting on it

A one-line acknowledgement that the job has started is fine, because that is
what they asked for - "Training is running." is right; "Training is running,
tracked as job train-2, watching the log for epoch markers, estimated 3h,
progress will appear in your statusline" is four sentences of noise about
plumbing.

## It costs nothing until a job proves slow

Long-running commands are tracked automatically, but **only after they have
earned it**. A command you run through Bash is wrapped so that it runs exactly
as it would have; if it finishes within 20 seconds nothing happens at all - no
job, no bar, no message, no tokens. You will not even know it was wrapped.

Only when a command is *still running* after that threshold does it get a bar.
Nothing else changes: the command keeps running in front of you, its output
keeps arriving exactly as it would have, and the call ends when the command
ends, with the command's own exit code. There is no note, because there is
nothing to tell you - you are watching the same command you would have been
watching anyway. Putting something in the background is your decision, not the
plugin's.
- **Give it an estimate if it is worth one.** The hook cannot guess a duration -
  that is the one thing only you can supply:

  ```bash
  agent-progress update <id> --eta 40m --note "what it is doing"
  ```

  Judge whether it is worth doing. A job that will be over in a minute never
  reaches the statusline anyway, so leave it alone and carry on. Spend the
  estimate on things that will actually run for a while.
- **Check what it is watching.** The handoff picks no monitor, so progress comes
  from reading markers in the log. If the job instead writes files, grows one
  file, or prints stage names, say so:

  ```bash
  agent-progress update <id> --monitor files --glob 'out/*.parquet' --total 500
  ```

You can still start a job yourself with `agent-progress run`, and it is better when
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

`agent-progress monitors` prints this list with examples.

How to choose: read the script or command, and skim any output it has already
produced (`head`, or `--help`). Ask what changes on disk or in the log as it
progresses. Prefer a signal that is **monotonic and bounded** — a count with a
known total beats a percentage, which beats named stages, which beats nothing.
`milestones` is the workhorse for scripts that just narrate what they are
doing; `probe` is the universal escape hatch when the state lives somewhere
else entirely (a database, a queue, an API, a remote host).

If nothing is observable, that is a fine answer — use `--monitor time` and the
bar runs on your estimate alone. That is still better than no bar.

## Jobs that are submitted rather than run

`sbatch`, `qsub` and `bsub` are handled for you. The submission is recognised,
the job id is read out of what it prints, and the queued job is tracked: its
progress comes from the file the scheduler writes, and its state comes from the
scheduler itself, so the bar finishes on its own and a job the cluster kills
arrives as a crash with the scheduler's own word for it - `OUT_OF_MEMORY`,
`TIMEOUT`, `NODE_FAIL`.

So `sbatch train.sbatch` needs nothing from you except, as ever, an estimate:

```bash
agent-progress update slurm-4242 --eta 6h
```

**A queued job has not started.** `agent-progress ls --json` reports it with
`"state": "queued"` and a `queue_reason_human` saying why it is waiting. Three
things follow, and getting them wrong is the usual way to be unhelpful about
cluster work:

- Do not report progress on it. There is none. It is waiting, not working, and
  saying "0% after an hour" describes a stall rather than a queue.
- Do not re-submit it, and do not run `squeue` or `sacct` to check on it. The
  watcher already asks every fifteen seconds while a job is queued, and the
  answer is in `ls --json` for free. Polling the scheduler yourself costs tokens
  and tells you less.
- Its estimate, until it starts, is the job's slurm `TimeLimit` - an upper
  bound, not a measurement. Replace it with a real one once the job is running
  and you can see what it is doing.

The wait is not counted as run time: when slurm reports the job started, the
clock is re-anchored to slurm's own `RunTime`. So `elapsed_s` on a running
scheduler job is time spent working, and you can quote it as such.

An array job is tracked as tasks finished out of tasks submitted, so it has a
real bar with no help from you.

To follow a job already in the queue, or one somebody else submitted:

```bash
agent-progress slurm 4242 --eta 6h
agent-progress slurm 4242 --interval 30s     # ask the scheduler more often
```

For any other queue, give it a command that prints the job's state - one of
COMPLETED / FAILED / RUNNING, or a bare exit code:

```bash
agent-progress start render --eta 2h --log render.log \
  --state-probe "kubectl get job/render -o jsonpath='{.status.conditions[0].type}'"
```

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

Keep the estimate to yourself - it goes in `--eta`, not into a message. If you
genuinely have no basis, **omit `--eta`** — the bar renders as an indeterminate sweep instead of
inventing a number. Do not fabricate confidence.

**If you expect it to finish in under about two minutes, do not track it at
all** — just run it normally and report the result. Short jobs are deliberately
hidden from the statusline (a bar that flashes past is noise), so tracking one
adds nothing. If the user explicitly wants a short job pinned anyway, pass
`--force-show`. A job you estimated as short but which is still running past the
threshold appears on its own, so an underestimate is not a problem.

## Step 3: launch

```bash
agent-progress run --name ingest --eta 40m --glob 'out/*.parquet' --total 500 --unit file \
  -- python pipeline.py --input raw/
```

`run` detaches the process, captures stdout and stderr, and tracks the pid.
Everything after `--` is the command. Prefer this over the Bash tool's own
background mode when the user wants to see progress — only `run` makes a bar.

For something already running:

```bash
agent-progress start reindex --pid 45123 --log /var/log/reindex.log --eta 3h
```

Then stop. The statusline carries it from there, and anything worth
notification are how they follow it - you do not need to narrate any of it.

## Other sessions

You may be one of several agents running at once, sharing one state file. What
you are shown is already filtered to your own session: the bars on your
statusline, the jobs described in your context, and the crashes you are handed
are yours. Do not reason about, report on, or clean up jobs you were not told
about - `agent-progress ls` does show every session's work, and another agent's
training run is not yours to cancel.

`rm --all` clears only your own jobs, which is what you want; it takes
`--everywhere` to touch another session's, and you should not.

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
agent-progress ls --json                      # status of everything, structured
agent-progress log ingest -n 40               # read recent output
agent-progress update ingest --eta 90m --note "hit the slow shard set"
agent-progress update ingest --monitor files --glob 'out/**/*.parquet'   # fix the monitor
```

Reasons to intervene: the log shows an error, a stall, or a retry loop; the job
has phases with very different costs and it just changed phase (add
`--reset-rate` so the old throughput is discarded); the monitor you picked is
clearly not tracking anything (`agent-progress ls --json` shows no movement and
`monitor_kind` is not `time`); or the user asks.

Do not re-run `agent-progress ls` repeatedly in one turn, and never sleep-loop
waiting for a job. If the user wants genuinely periodic reporting, use the
`/loop` skill.

## When a job finishes

**You will not be told.** A job ending is shown to the user beside the
conversation, not handed to you: it costs the conversation nothing and
interrupts nothing. So do not wait for it, do not poll for it, and do not
mention it unless the user brings it up.

If they do ask - "did it finish?", "what did it get?" - then look:

```
agent-progress ls               what is running, and what ended recently
agent-progress log <id> -n 60   what it printed
```

That is also the only reason to run those commands. A job observes itself.

## When a job crashes

A job that dies is shown to the user beside the conversation, the same way a
finished one is, and you are not interrupted for it. What they see looks like:

```
💀 A tracked job CRASHED while you were working: 'trainer'
  SIGKILL - killed outright - most often the OOM killer after 04:12
  command: python train.py --epochs 50
  log: ~/.claude/agent-progress/logs/trainer.log
  last output: ...
```

When you get one, **tell the user immediately** — before continuing whatever you
were doing, and even if it is unrelated to the current task. Then:

1. Say plainly what died and how long it ran.
2. Read the cause out of the last output you were given. `SIGKILL` usually means
   the OOM killer; `SIGSEGV` a native crash; a plain non-zero exit means the
   traceback in the log tail is the real story. Read more with
   `agent-progress log <id> -n 60` if the tail is not enough.
3. Suggest a concrete fix if the cause is clear (smaller batch size, more
   memory, a missing file).
4. **Do not re-run the job without asking.** It may have burned hours, and
   re-running it blindly can repeat an expensive failure.

Several jobs can die at once — an out-of-memory or a failing GPU takes
everything on the machine — and they arrive together in one report. Say that
several died and give the shared cause once, rather than working through them
one at a time as though they were unrelated. If the report ends by saying more
are still queued, tell the user that too: the number that died is the number it
names, not the number described.

A report may say the job belonged to **another session** that has since exited.
Pass it on as that: someone else's job died, you did not start it, and it is not
yours to re-run.

Each report is delivered once, so do not wait for a repeat — act on it when you
see it. A job the user cancelled deliberately is not a crash and is never
reported this way.

## Reporting status

`agent-progress ls --json` gives `percent`, `elapsed_human`, `remaining_human`,
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
agent-progress done ingest
agent-progress fail ingest --exit-code 137 --note "OOM killed"
agent-progress cancel ingest        # also SIGTERMs the process
```

## Appearance

Everything about the bar is configurable — width, style, which fields appear,
colors, thresholds. If the user asks for it to look different, do not edit the
script: `agent-progress config` lists every setting with its default, `agent-progress
config --set key=value` changes one, and `agent-progress preview` renders sample
bars so they can see the result immediately. `agent-progress config --preset
minimal|rich|tqdm|plain|quiet` covers the common requests in one step.
