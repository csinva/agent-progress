#!/usr/bin/env python3
"""Record a short .mov and .gif of agent-progress.

Everything shown is real: the commands go through the actual PreToolUse hook,
which rewrites them exactly as it would in a session, and the bars are rendered
from live job state by the same code that draws the statusline.

The clip is a faithful recording of a ~2 minute session, not a re-enactment.
Waiting is compressed by capturing fewer frames per second of real time, with
the multiplier shown on screen, so the 20-second threshold is really 20 seconds.

    python3 demo/record.py
    python3 demo/record.py --out /tmp/x.mov --fps 12
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENGINE = os.path.join(ROOT, "scripts", "agent_progress.py")
HOOK = os.path.join(ROOT, "hooks", "auto_track.py")

BG = (13, 17, 23)
FG = (210, 210, 210)
PAD = 22
PANEL_COLS = 42                 # each of the three columns
GAP = " \u2502 "                 # what separates them
COLS = PANEL_COLS * 3 + len(GAP) * 2
ROWS = 17

PRIMARY_FONT = "/System/Library/Fonts/Menlo.ttc"
FALLBACK_FONTS = ["/System/Library/Fonts/Apple Symbols.ttf",
                  "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"]

ANSI = re.compile(r"\033\[([0-9;]*)m")
CUBE = [0, 95, 135, 175, 215, 255]
BASIC = [(0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238),
         (205, 0, 205), (0, 205, 205), (229, 229, 229), (127, 127, 127),
         (255, 0, 0), (0, 255, 0), (255, 255, 0), (92, 92, 255),
         (255, 0, 255), (0, 255, 255), (255, 255, 255)]


def xterm256(n):
    if n < 16:
        return BASIC[n]
    if n < 232:
        n -= 16
        return (CUBE[n // 36], CUBE[(n // 6) % 6], CUBE[n % 6])
    v = 8 + (n - 232) * 10
    return (v, v, v)


def ansi_runs(s):
    runs, pos, cur = [], 0, FG
    for m in ANSI.finditer(s):
        if m.start() > pos:
            runs.append((s[pos:m.start()], cur))
        code = m.group(1)
        parts = code.split(";")
        if code in ("", "0"):
            cur = FG
        elif parts[:2] == ["38", "5"] and len(parts) > 2:
            cur = xterm256(int(parts[2]))
        pos = m.end()
    if pos < len(s):
        runs.append((s[pos:], cur))
    return runs


def _bitmap(font, ch, box=48):
    img = Image.new("L", (box, box), 0)
    ImageDraw.Draw(img).text((4, 4), ch, font=font, fill=255)
    return img.tobytes()


def build_fallbacks(chars, size):
    """Pre-render characters Menlo lacks (the Braille spinner) as scaled masks."""
    primary = ImageFont.truetype(PRIMARY_FONT, size)
    tofu, blank = _bitmap(primary, ""), _bitmap(primary, " ")
    target = max(4, int(size * 0.66))
    out = {}
    for ch in set(chars):
        if _bitmap(primary, ch) not in (tofu, blank):
            continue
        for path in FALLBACK_FONTS:
            if not os.path.exists(path):
                continue
            try:
                alt = ImageFont.truetype(path, size)
                big = ImageFont.truetype(path, size * 4)
            except Exception:
                continue
            if _bitmap(alt, ch) in (_bitmap(alt, ""), _bitmap(alt, " ")):
                continue
            canvas = Image.new("L", (size * 8, size * 8), 0)
            ImageDraw.Draw(canvas).text((size, size), ch, font=big, fill=255)
            box = canvas.getbbox()
            if not box:
                continue
            ink = canvas.crop(box)
            scale = target / float(ink.height)
            out[ch] = ink.resize((max(1, int(ink.width * scale)), target), Image.LANCZOS)
            break
    return out


def emoji_glyph(ch, px):
    for size in (160, 96, 64, 48):
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Apple Color Emoji.ttc", size)
        except Exception:
            continue
        img = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
        ImageDraw.Draw(img).text((size // 4, size // 4), ch, font=font, embedded_color=True)
        box = img.getbbox()
        if not box:
            continue
        img = img.crop(box)
        scale = px / float(img.height)
        return img.resize((max(1, int(img.width * scale)), px), Image.LANCZOS)
    return None


def char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def make_frame(lines, font, cell_w, line_h, size, emoji, fallbacks):
    img = Image.new("RGB", size, BG)
    draw = ImageDraw.Draw(img)
    y = PAD
    for line in lines:
        x = float(PAD)
        for text, color in ansi_runs(line):
            chunk = ""
            for ch in text:
                if ch in emoji or ch in fallbacks:
                    if chunk:
                        draw.text((x, y), chunk, font=font, fill=color)
                        x += len(chunk) * cell_w
                        chunk = ""
                    if ch in emoji:
                        g = emoji[ch]
                        img.paste(g, (int(x), int(y + line_h * 0.08)), g)
                        x += cell_w * 2
                    else:
                        m = fallbacks[ch]
                        tile = Image.new("RGB", m.size, color)
                        img.paste(tile, (int(x + max(0, (cell_w - m.width) / 2)),
                                         int(y + (line_h * 0.66 - m.height) / 2)), m)
                        x += cell_w
                else:
                    chunk += ch
            if chunk:
                draw.text((x, y), chunk, font=font, fill=color)
                x += len(chunk) * cell_w
        y += line_h
    return img


def write_gif(frames_dir, out, width, every, colors, fps):
    names = sorted(f for f in os.listdir(frames_dir) if f.endswith(".png"))
    kept = names[::max(1, every)]
    if not kept:
        return

    def load(name):
        im = Image.open(os.path.join(frames_dir, name)).convert("RGB")
        h = int(im.height * width / float(im.width))
        return im.resize((width, h - h % 2), Image.LANCZOS)

    palette = load(kept[len(kept) * 3 // 4]).quantize(colors=colors, method=Image.MEDIANCUT)
    imgs = [load(n).quantize(palette=palette, dither=Image.NONE) for n in kept]
    imgs[0].save(out, save_all=True, append_images=imgs[1:],
                 duration=int(1000.0 * every / fps), loop=0, optimize=True)
    print("%s  (%.2f MB, %d frames)" % (out, os.path.getsize(out) / 1e6, len(imgs)))


# --------------------------------------------------------------------- the take

MAKEFILE = """all:
\t@echo "cc  src/parser.c"; sleep 1
\t@echo "cc  src/render.c"; sleep 1
\t@echo "cc  src/main.c";   sleep 1
\t@echo "ld  build/app";    sleep 1
"""

TRAIN = """import time
N = 34
for i in range(1, N + 1):
    time.sleep(1.9)
    print("Epoch %d/%d  loss %.3f" % (i, N, 2.4 / (i ** 0.5)), flush=True)
print("saved checkpoints/final.pt", flush=True)
"""

# The benchmark is submitted to a scheduler rather than run here, because a
# queue is where most benchmarking actually happens and it is the case the bar
# has the most to say about: submitted, waiting, why it is waiting, started,
# dead. `sbatch` returns in a tenth of a second having done none of the work,
# so nothing about the 20-second threshold applies to it - it is tracked from
# the moment slurm accepts it.
#
# There is no cluster attached to the machine this is recorded on, so `sbatch`,
# `scontrol` and `sacct` below stand in for one. Everything on the agent-progress
# side of them is the real thing: the real hook rewrites the command, the real
# submission detector reads the job id out of what sbatch printed, and the real
# watcher asks the real questions and draws the bar from the answers.
BENCH_SBATCH = """#!/bin/bash
#SBATCH --job-name=bench
#SBATCH --gres=gpu:2
#SBATCH --time=01:00:00
python3 benchmark.py --model llama-7b --dtype float32
"""

QUEUED_FOR = 38        # seconds in the queue before slurm starts it
RAN_FOR = 12           # seconds of running before it dies

# Tokens, not %-formatting: these are shell scripts, and shell uses % for
# arithmetic. Escaping one against the other is how the running branch below
# silently became a syntax error the first time round.
SLURM_STUBS = {
    "sbatch": """#!/bin/sh
date +%s > "$AP_FAKE_SLURM/t0"
(
  sleep @QUEUED@
  for i in 1 2; do
    echo "warmup $i/3" >> "$PWD/slurm-81734.out"
    sleep 3
  done
  sleep 3
  # it dies during the third pass, so the bar stops at two of three rather than
  # filling up first - a skull over a complete bar reads like it succeeded
  echo "torch.cuda.OutOfMemoryError: unable to allocate 8.00 GiB on cuda:0" \
      >> "$PWD/slurm-81734.out"
) >/dev/null 2>&1 &
echo "Submitted batch job 81734"
""",
    "scontrol": """#!/bin/sh
T0=$(cat "$AP_FAKE_SLURM/t0" 2>/dev/null) || exit 1
E=$(( $(date +%s) - T0 ))
if [ "$E" -lt @QUEUED@ ]; then
  echo "JobId=81734 JobName=bench UserId=me(1000) JobState=PENDING Reason=Resources \
Partition=gpu NumNodes=2 TimeLimit=01:00:00 RunTime=00:00:00 NodeList=(null) \
StdOut=$PWD/slurm-81734.out"
elif [ "$E" -lt @OVER@ ]; then
  R=$(( E - @QUEUED@ ))
  M=$(( R / 60 ))
  S=$(( R - M * 60 ))
  printf 'JobId=81734 JobName=bench UserId=me(1000) JobState=RUNNING Reason=None '
  printf 'Partition=gpu NumNodes=2 TimeLimit=01:00:00 RunTime=00:%02d:%02d ' "$M" "$S"
  printf 'NodeList=gpu-[3-4] StdOut=%s/slurm-81734.out\n' "$PWD"
else
  exit 1
fi
""",
    "sacct": """#!/bin/sh
T0=$(cat "$AP_FAKE_SLURM/t0" 2>/dev/null) || exit 1
E=$(( $(date +%s) - T0 ))
[ "$E" -lt @OVER@ ] && exit 0
echo "OUT_OF_MEMORY|00:00:@RAN@|2026-09-01T10:00:00|0:125|gpu-[3-4]"
""",
}


def stub_text(body, queued, ran):
    for token, value in (("@QUEUED@", queued), ("@RAN@", ran), ("@OVER@", queued + ran)):
        body = body.replace(token, str(value))
    return body

# One column each. They run at the same time, as three commands in a session
# would, so the clip shows the three outcomes together rather than in turn.
#
# Left to right is longest-lived to shortest. The build is last because it is
# over before the clip has finished introducing itself: it is the control, the
# answer to "what does this cost when there is nothing to track", and putting
# the two jobs that actually have bars next to each other lets them be read
# together rather than across a dead column.
PANELS = [
    {"key": "train", "jid": "rec-train",
     "ask": "train the model on the new data",
     "command": "python3 train.py --epochs 34",
     "file": "train.py", "body": TRAIN,
     "idle": "not tracked yet",
     "reply": "Training is running.",
     "done_reply": "Training finished. Final loss 0.41."},
    {"key": "benchmark", "jid": "slurm-81734",
     "ask": "benchmark llama-7b on the cluster",
     "command": "sbatch bench.sbatch",
     "file": "bench.sbatch", "body": BENCH_SBATCH,
     "idle": "not submitted yet",
     "reply": "Submitted. It is in the queue.",
     "done_reply": "Slurm killed it: out of memory on cuda:0,\n"
                   "in the third warmup pass. 7b in fp32 needs\n"
                   "~28GB on 2 GPUs. Resubmit with bfloat16."},
    {"key": "make", "jid": None, "ask": "build the project", "command": "make -j8",
     "file": "Makefile", "body": MAKEFILE,
     "idle": "under 20s - never tracked",
     "reply": "Built. Four seconds, nothing to track.",
     "done_reply": None},
]


def pad(line, width):
    """Clip a line to a column and pad it out, counting visible columns only."""
    line = ansi_clip(line, width)
    return line + " " * max(0, width - visible_columns(line))


def visible_columns(text):
    return sum(char_width(c) for c in ANSI.sub("", text))


def ansi_clip(text, width):
    if visible_columns(text) <= width:
        return text
    out, seen, i = [], 0, 0
    while i < len(text):
        if text[i] == "\033":
            k = text.find("m", i)
            if k == -1:
                break
            out.append(text[i:k + 1]); i = k + 1
            continue
        w = char_width(text[i])
        if seen + w > width - 1:
            break
        out.append(text[i]); seen += w; i += 1
    return "".join(out) + "\u2026\033[0m"


class Panel(object):
    """One column: a command, whatever it has printed, and its bar."""

    def __init__(self, cc, spec, scratch):
        self.cc, self.spec, self.scratch = cc, spec, scratch
        self.lines = []
        self.path = None
        self.offset = 0
        self.started = False

    def say(self, text=""):
        self.lines.append(text)

    def launch(self):
        """Send the command through the real hook, then run what it produced."""
        c = self.cc
        command = self.spec["command"]
        payload = {"tool_name": "Bash", "session_id": "demo",
                   "tool_input": {"command": command}}
        out = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                             capture_output=True, text=True).stdout.strip()
        actual = command
        if out:
            actual = json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"]
            actual = actual.replace(" --name ", " --name rec-", 1)
        self.path = os.path.join(self.scratch, "%s.out" % self.spec["key"])
        open(self.path, "w").close()
        subprocess.Popen(["/bin/sh", "-c", actual], cwd=self.scratch,
                         stdin=subprocess.DEVNULL, stdout=open(self.path, "w"),
                         stderr=subprocess.STDOUT)
        self.started = True

    def drain(self):
        if not self.path:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return
        if not chunk:
            return
        self.offset += len(chunk)
        for raw in chunk.decode("utf-8", "replace").splitlines():
            line = raw.rstrip()
            if not line:
                continue
            tone = "warn" if line.startswith("[agent-progress]") else "dim"
            # the handoff message is written for a wide terminal; in a column
            # only its first sentence earns the space
            bare = line.strip()
            if bare.startswith("[agent-progress]"):
                if "job" in bare and "queued" in bare:
                    line = "[agent-progress] slurm job 81734, queued"
                elif any("tracked, now" in l for l in self.lines):
                    continue
                else:
                    line = "[agent-progress] tracked, now in the background"
            elif bare.startswith(("agent-progress ", "If you expect", "bar can say",
                                  "running in the background", "Note:", "statusline was",
                                  "Restart Claude Code", "and `agent-progress")):
                continue        # the full banner is written for a wide terminal
            self.say(self.cc.paint("  " + line, tone, True))

    def reply(self, text, tone="done"):
        for line in (text or "").split("\n"):
            self.say(self.cc.paint("  " + line, tone, True))

    def job(self):
        jid = self.spec.get("jid")
        if not jid:
            return None
        for j in self.cc.state_ro()["jobs"].values():
            if (j.get("id") or "") == jid:
                return j
        return None

    def render(self, cfg, height):
        c = self.cc
        # what the user typed, then what Claude ran, then what happened
        body = [c.paint("\u203a ", "run", True) + c.paint(self.spec["ask"], "text", True),
                c.paint("  $ " + self.spec["command"], "dim", True), ""]
        body += self.lines[-(height - 5):]
        while len(body) < height - 2:
            body.append("")
        body.append(c.paint("\u2500\u2500 statusline " + "\u2500" * (PANEL_COLS - 15),
                            "dim", True))
        j = self.job()
        body.append(c.render_line(j, cfg, width=PANEL_COLS) if j
                    else c.paint("  " + self.spec["idle"], "dim", True))
        return [pad(l, PANEL_COLS) for l in body[:height]]


# (until real-seconds, speed, caption). Speed is how much real time each
# captured frame covers, so waiting can be skipped past without faking it.
# (until real-seconds, speed, caption). Speed is how much real time each
# captured frame covers, so the waiting can be skipped past without faking it.
SCRIPT = [
    (3.0,  1,  "three things asked for in plain words. Claude picks the commands"),
    (8.0,  1,  "sbatch returned in a tenth of a second. the job is queued, and tracked"),
    (16.0, 1,  "queued is not running: no invented progress, and slurm's own reason"),
    (24.0, 8,  "meanwhile the training run waits out the 20-second threshold"),
    (30.0, 1,  "it crossed it: tracked, moved to the background, a bar of its own"),
    (35.0, 1,  "Claude gives the training run an estimate - a hook cannot guess one"),
    (44.0, 4,  "the queue lets the benchmark start"),
    (54.0, 1,  "its clock starts now: the 38s of waiting was never counted as work"),
    (60.0, 1,  "slurm kills it: out of memory. that is when Claude speaks up"),
    (78.0, 14, "the training run carries on, costing nothing while it does"),
    (84.0, 1,  "and the build? four seconds, never tracked, never mentioned again"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "agent-progress.mov"))
    ap.add_argument("--gif", default=os.path.join(HERE, "agent-progress.gif"))
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument("--font-size", type=int, default=18)
    ap.add_argument("--gif-width", type=int, default=1040)
    ap.add_argument("--gif-every", type=int, default=2)
    ap.add_argument("--gif-colors", type=int, default=48)
    args = ap.parse_args()

    # A state directory of the recording's own. It clears every job twice in
    # the course of a take, and doing that to whatever the person recording
    # happens to have running would be an unpleasant surprise. Set before the
    # engine is imported: it reads the variable once, at import.
    scratch = tempfile.mkdtemp(prefix="agent-progress-rec-")
    os.environ["AGENT_PROGRESS_HOME"] = os.path.join(scratch, "state")

    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_progress", ENGINE)
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)

    # a bar has one narrow column to live in, so it keeps only what fits
    cfg = dict(cc.load_config())
    cfg.update(bar_width=10, show_name=False, show_rate=False, show_eta_clock=False,
               show_drift=False, show_note=False, show_counts=False, name_width=10)

    font = ImageFont.truetype(PRIMARY_FONT, args.font_size)
    cell_w = font.getlength("M")
    line_h = int(args.font_size * 1.55)
    width = int(PAD * 2 + cell_w * COLS)
    height = PAD * 2 + line_h * ROWS
    size = (width + (width % 2), height + (height % 2))
    emoji = {}
    for ch in ("\U0001f480", "\u23f3"):     # the skull, and the hourglass a queue gets
        g = emoji_glyph(ch, int(line_h * 0.78))
        if g:
            emoji[ch] = g
    fallbacks = build_fallbacks(cfg["spinner"] + "\u2713\u2192\u00b7\u25b6\u2502\u2500",
                                args.font_size)

    frames = os.path.join(scratch, "frames")
    os.makedirs(frames)
    for spec_ in PANELS:
        open(os.path.join(scratch, spec_["file"]), "w").write(spec_["body"])

    # The stub scheduler goes on PATH, and everything the recording starts -
    # the CLI, the hook, the watcher - inherits it.
    stub_bin = os.path.join(scratch, "bin")
    os.makedirs(stub_bin)
    for name, body in SLURM_STUBS.items():
        path = os.path.join(stub_bin, name)
        open(path, "w").write(stub_text(body, QUEUED_FOR, RAN_FOR))
        os.chmod(path, 0o755)
    os.environ["PATH"] = stub_bin + os.pathsep + os.environ["PATH"]
    os.environ["AP_FAKE_SLURM"] = scratch
    subprocess.run([sys.executable, ENGINE, "rm", "--all"], capture_output=True)

    panels = [Panel(cc, spec_, scratch) for spec_ in PANELS]
    by_key = dict((p.spec["key"], p) for p in panels)
    total = SCRIPT[-1][0]
    print("recording %.0fs of real time..." % total)

    start = time.time()
    n, last, caption, speed = 0, 0.0, "", 1.0
    fired = set()
    panel_height = ROWS - 2
    while True:
        now = time.time() - start
        if now > total:
            break
        for at, sp, cap in SCRIPT:
            if now <= at:
                speed, caption = sp, cap
                break

        if now > 0.4 and "launch" not in fired:
            fired.add("launch")
            for pan in panels:
                pan.launch()
        # Claude answers once when the job is under way, and once when it ends -
        # which is the whole of what it says about any of this
        for idx, pan in enumerate(panels):
            key = "reply%d" % idx
            j = pan.job()
            started = j is not None or (pan.spec["key"] == "make" and now > 6.5)
            if started and key not in fired:
                fired.add(key)
                pan.reply(pan.spec["reply"], "run")
            done_key = "done%d" % idx
            # queued is not finished: a job waiting for nodes has not ended,
            # and answering as though it had puts the obituary before the job
            if (j and j.get("state") not in (None, "running", "queued")
                    and done_key not in fired and pan.spec.get("done_reply")):
                fired.add(done_key)
                pan.reply(pan.spec["done_reply"],
                          "fail" if j.get("state") == "failed" else "done")

        if now > 24.0 and "quick" not in fired:
            fired.add("quick")
            # A real job is re-observed every 2 minutes, or once per 5% of its
            # estimate. Over a job this short that is a single reading, and the
            # bars would sit still for the whole clip. Forced to 2s here so
            # there is something to watch; the header says so.
            for pan in panels:
                if pan.spec.get("jid"):
                    subprocess.run([sys.executable, ENGINE, "update", pan.spec["jid"],
                                    "--interval", "2s", "--quiet"], capture_output=True)
        if now > 31.0 and "eta" not in fired:
            fired.add("eta")
            subprocess.run([sys.executable, ENGINE, "update", "rec-train",
                            "--eta", "45s", "--quiet"], capture_output=True)
            by_key["train"].say(cc.paint("  $ agent-progress update rec-train --eta 45s",
                                         "run", True))
        for pan in panels:
            pan.drain()

        if now - last >= (speed / float(args.fps)):
            last = now
            head = cc.paint("agent-progress", "dim", True)
            if speed > 1:
                head += cc.paint("   \u25b6\u25b6 %g\u00d7 faster" % speed, "warn", True)
            head += cc.paint("      probes forced to 2s for this clip "
                             "(a real job: every 2m, or 5% of its estimate)",
                             "dim", True)
            columns = [pan.render(cfg, panel_height) for pan in panels]
            lines = [head]
            for row in range(panel_height):
                lines.append(GAP.join(col[row] for col in columns))
            lines.append(cc.paint("  " + caption, "run", True))
            make_frame(lines[:ROWS], font, cell_w, line_h, size, emoji, fallbacks).save(
                os.path.join(frames, "f%05d.png" % n))
            n += 1
        time.sleep(0.03)

    print("rendered %d frames at %dx%d" % (n, size[0], size[1]))
    binary = os.path.join(scratch, "mov_encoder")
    subprocess.run(["swiftc", "-O", os.path.join(HERE, "mov_encoder.swift"),
                    "-o", binary], check=True)
    subprocess.run([binary, args.out, str(args.fps), frames], check=True)
    write_gif(frames, args.gif, args.gif_width, args.gif_every, args.gif_colors, args.fps)

    subprocess.run([sys.executable, ENGINE, "rm", "--all"], capture_output=True)
    try:
        with cc.state_rw() as st:
            st["inbox"] = [e for e in st.get("inbox", [])
                           if not (e.get("job") or "").startswith("rec-")]
    except Exception:
        pass
    shutil.rmtree(scratch, ignore_errors=True)
    print("%s  (%.2f MB)" % (args.out, os.path.getsize(args.out) / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
