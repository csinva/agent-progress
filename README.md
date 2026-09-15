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
Because a quick run now costs nothing, the detector can afford to be broad. A
command is caught when it is backgrounded, when it is given a timeout of two
minutes or more, or when it matches one of 38 patterns — training scripts,
`torchrun`, `accelerate`, `deepspeed`, sweeps, `spark-submit`, `terraform`,
`ansible`, `docker build`, `rsync`, `aws s3 sync`, model downloads, `dvc`,
`dbt`, `pg_restore`, `git clone`, and ordinary work like `pytest`, `make`,
`cargo build`, `npm test`, `go test`. Catching `pytest` is free when the suite
takes four seconds, and useful when it takes four minutes.

```bash
agent-progress autotrack 'pytest tests/'
#   TRACK        pytest tests/
#                it looks like a pytest run
#   becomes      agent-progress exec --name pytest --after 20 --shell 'pytest tests/'
#                only tracked if still running after 20s
```

## Custom configuration

61 settings, each with a default, a type, a valid range and a one-line
explanation:

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
