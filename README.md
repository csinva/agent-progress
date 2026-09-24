<h1 align="center">agent-progress</h1>
<p align="center">tqdm-style progress bars in the Claude Code statusline, for any long-running job.</p>

<p align="center">
  <a href="#install">install</a> •
  <a href="#what-counts-as-progress">monitors</a> •
  <a href="#when-a-job-crashes">crashes</a> •
  <a href="#configuration">configuration</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-mit-blue.svg">
  <img src="https://img.shields.io/badge/python-3.8+-blue">
  <img src="https://img.shields.io/badge/claude%20code-plugin-orange">
</p>

<p align="center">
  <img src="demo/agent-progress.gif" width="100%" alt="two jobs streaming their own output under progress bars, with their endings shown beside the conversation">
</p>

After claude runs a script for over a minute, `agent-progress` calls an LLM to (1) estimate a job's ETA and (2) write a script to monitor it.
Then, the script is run and updates a progress bar (not using any more tokens). 

Minor: if the job fails the session gets alerted and can respond automatically. If the job goes past the ETA, the agent estimates a new ETA.

---

## Install

Just point Claude code to this repo or run the following:

```bash
git clone https://github.com/csinva/agent-progress ~/.claude/skills/agent-progress
~/.claude/skills/agent-progress/scripts/install-statusline.sh
```


## Features
A command is caught when it is backgrounded, when it is given a timeout of two
minutes or more (by the tool, or by a `timeout 2h ...` in front of it), when it
runs a shell script whose contents would be caught, or when it matches
one of 65 patterns:

- **training and evaluation** — scripts named for the work (`train`, `eval`,
  `predict`, `embed`, `preprocess`, `generate`, ...), `--epochs`/`--max_steps`
  flags, `torchrun`, `accelerate`, `deepspeed`, `lm_eval`, sweeps, hydra
  `--multirun`, `Rscript`, `julia`, `matlab -batch`
- **clusters and workflows** — `sbatch`, `srun`, `qsub`, `bsub`, `sky`,
  `modal`, `nextflow`, `snakemake`, `papermill`, `spark-submit`
- **moving data** — `wget`, `curl -o`, `hf download`, `rsync`, `rclone`,
  `scp`, `aws s3`, `gsutil`, `git lfs`, `docker pull`, `ollama pull`,
  `git clone`
- **installing** — `pip`, `uv sync`, `conda`/`mamba`, `npm ci`, `apt`, `brew`
- **builds, tests and checks** — `pytest`, `make`, `cargo`, `go test`, `jest`,
  `playwright`, `mypy`, `tsc`, `pre-commit`, `docker build`, `bazel`, `ninja`
- **everything else slow** — `ffmpeg`, `tar`, `zstd`, `latexmk`, `sphinx-build`,
  `psql -f`, `pg_restore`, `terraform`, `helm`, `dbt`, `dvc`, `xargs -P`

Catching `pytest` is free when the suite takes four seconds, and useful when it
takes four minutes. Servers and watchers — `uvicorn`, `jupyter lab`,
`npm run dev`, `tail -f`, anything with `--watch` — are never tracked: a bar
for something that never ends would never end either.

```bash
agent-progress autotrack 'pytest tests/'
#   TRACK        pytest tests/
#                it looks like a pytest run
#   becomes      agent-progress exec --name pytest --after 20 --shell 'pytest tests/'
#                only tracked if still running after 20s
```

A command that sends itself to the background — `nohup python train.py >
train.log 2>&1 &` — runs exactly as written, and is followed by the pid it
leaves in `$!` and the file its output goes to.

Work handed to a scheduler is followed on the scheduler's own word, however it
was submitted: `sbatch job.sbatch`, `JOB=$(sbatch --parsable job.sbatch)` (the
id is captured and never printed, so slurm is asked what was submitted from
here while the command ran), a loop that submits twenty, or a script that
submits them for you.

## What counts as progress

Each job is watched by one monitor, chosen when it starts:

| monitor | watches |
| --- | --- |
| `auto` | the log, for any known progress marker — tqdm, `epoch 3/10`, `step 400/1000`, percentages (the default) |
| `log` | the log, with your own regex: `--pattern 'done (?P<step>\d+)/(?P<total>\d+)'` |
| `milestones` | named stages appearing in the log, each an equal slice |
| `files` | output files appearing: `--glob 'out/shard-*.parquet' --total 500` |
| `size` | a file or directory growing toward a size: `--path out/index.bin --target-size 12GB` |
| `probe` | any command that prints `k/N`, `k` or `NN%` — a database count, a queue, a remote host |
| `time` | nothing observable: the bar runs on the estimate alone |

`agent-progress monitors` prints the same, with the flags each one takes.

## Custom configuration

Presets bundle common combinations:

| preset | effect |
| --- | --- |
| `minimal` | bar and percentage only |
| `rich` | every field, wider bar, five jobs |
| `tqdm` | tqdm-faithful |
| `plain` | ascii, no color |
| `quiet` | only jobs over ten minutes, one at a time, no notifications |
| `guided` | ask before taking a command over (`auto_track=instruct`) |
| `manual` | never take one over (`auto_track=off`) |
| `eager` | start tracking from the first second |

```bash
agent-progress config --preset minimal
```

See and modify options:

```bash
agent-progress config                       # the whole table, * marks what you changed
agent-progress config --set bar_width=30 --set style=tqdm
agent-progress config --unset bar_width     # back to the default
agent-progress config --reset               # back to all defaults
agent-progress config --edit                # open the JSON in $EDITOR
```

Values are validated on the way in — a bad range, an invalid choice or a
misspelled key is rejected with the reason and a suggestion:

```
$ agent-progress config --set bar_widht=30
unknown setting 'bar_widht' - did you mean bar_width, name_width, note_width?
```

See changes before keeping them:

```bash
agent-progress preview                      # sample bars in every state
agent-progress preview --set style=dots     # try a setting without saving it
agent-progress preview --colors             # the 256-color codes
```

Any setting can be overridden for one command via the environment:

```bash
AGENT_PROGRESS_BAR_WIDTH=40 AGENT_PROGRESS_STYLE=bars agent-progress ls
```

`NO_COLOR` is honored.

| group | settings |
| --- | --- |
| **visibility** | `min_duration_seconds`, `max_jobs`, `keep_done_seconds`, `keep_done_prompts`, `keep_failed_seconds`, `prune_after_hours`, `show_context_line`, `scope` |
| **cadence** | `min_interval_seconds`, `interval_fraction` |
| **bar shape** | `style`, `bar_width`, `name_width`, `fill_char`, `track_char`, `left_cap`, `right_cap`, `spinner`, `spinner_fps`, `glyph_done`, `glyph_failed`, `glyph_cancelled`, `glyph_stalled`, `glyph_queued` |
| **fields** | `show_spinner`, `show_name`, `show_percent`, `show_counts`, `show_clock`, `show_rate`, `show_eta_clock`, `show_drift`, `show_note`, `note_width`, `clock_format` |
| **color** | `color`, `color_running`, `color_done`, `color_failed`, `color_warn`, `color_dim`, `color_track`, `color_text` |
| **estimation** | `blend_full_at`, `rate_window`, `rate_min_span`, `drift_threshold` |
| **behavior** | `notify`, `notify_sound_ok`, `notify_sound_fail`, `crash_alert`, `announce_done`, `report_style`, `context_min_interval_seconds`, `crash_handover_seconds` |
| **auto** | `auto_track`, `auto_track_after_seconds`, `auto_track_timeout_seconds`, `auto_track_background`, `auto_track_patterns`, `auto_track_ignore` |

Styles are `blocks`, `tqdm`, `ascii`, `dots`, `bars`, and any of them can be
overridden character by character:

```bash
agent-progress config --set style=bars --set fill_char=▓ --set track_char=░
```

---

## License

MIT
