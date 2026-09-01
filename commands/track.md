---
description: "Run a long job in the background with a live progress bar and a self-correcting ETA"
argument-hint: "<command to run, e.g. python pipeline.py --input raw/>"
---

# Track a long-running job

Long jobs are normally caught automatically, so this command is for when the
user wants to be explicit, or wants to track something the detector would let
through (`agent-tqdm autotrack '<command>'` says which).

The user wants to run this with a progress bar:

```
$ARGUMENTS
```

Follow the `agent-tqdm` skill. In short:

1. If `$ARGUMENTS` is empty, ask what to run — do not guess.
2. **Decide how this job's progress can be observed.** Read the script or
   command. Does it print a counter, narrate named stages, write output files,
   grow one file, or expose nothing? Pick the matching monitor
   (`auto` / `log` / `milestones` / `files` / `size` / `probe` / `time`) —
   `agent-tqdm monitors` lists them. This is the important decision; do not
   assume the job prints epochs.
3. **Estimate the duration.** Check for evidence of a previous run first, then
   the size of the work, then the hardware. Roughly right is fine. If you have
   no basis at all, omit `--eta`.
4. Tell the user your estimate, the one-line reasoning, and what you will be
   watching to measure progress.
5. Launch it:
   `agent-tqdm run --name <short-name> --eta <estimate> <monitor flags> -- $ARGUMENTS`
6. Confirm with `agent-tqdm ls` and give the expected wall-clock finish time.

Then stop. The job is detached and re-observes itself on a timer. Do not wait on
it, poll it, or sleep-loop.
