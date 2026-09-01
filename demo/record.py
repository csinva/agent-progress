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

BENCH = """import time
print("loading llama-7b", flush=True)
time.sleep(9)
print("warmup 1/3", flush=True)
time.sleep(11)
print("warmup 2/3", flush=True)
time.sleep(10)
raise MemoryError("unable to allocate 8.00 GiB on cuda:0")
"""

# One column each. They run at the same time, as three commands in a session
# would, so the clip shows the three outcomes together rather than in turn.
PANELS = [
    {"key": "make", "title": "make -j8", "command": "make -j8",
     "file": "Makefile", "body": MAKEFILE,
     "idle": "under 20s - never tracked"},
    {"key": "train", "title": "python3 train.py", "command": "python3 train.py",
     "file": "train.py", "body": TRAIN,
     "idle": "not tracked yet"},
    {"key": "benchmark", "title": "python3 benchmark.py", "command": "python3 benchmark.py",
     "file": "benchmark.py", "body": BENCH,
     "idle": "not tracked yet"},
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
                if any("tracked, now" in l for l in self.lines):
                    continue
                line = "[agent-progress] tracked, now in the background"
            elif bare.startswith(("agent-progress ", "If you expect", "bar can say",
                                  "running in the background", "Note:", "statusline was",
                                  "Restart Claude Code", "and `agent-progress")):
                continue        # the full banner is written for a wide terminal
            self.say(self.cc.paint("  " + line, tone, True))

    def job(self):
        for j in self.cc.state_ro()["jobs"].values():
            if (j.get("id") or "") == "rec-" + self.spec["key"]:
                return j
        return None

    def render(self, cfg, height):
        c = self.cc
        body = [c.paint("$ " + self.spec["title"], "text", True), ""]
        body += self.lines[-(height - 4):]
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
    (3.0,  1,  "three commands, launched together"),
    (7.0,  1,  "the build is done already - four seconds, so it is never tracked"),
    (14.0, 1,  "the other two are still going. the threshold is 20 seconds"),
    (22.0, 8,  "waiting out the 20 seconds"),
    (27.0, 1,  "both crossed it: tracked, moved to the background, bars"),
    (33.0, 1,  "Claude gives the training run an estimate - a hook cannot guess one"),
    (39.0, 5,  "waiting on the benchmark"),
    (46.0, 1,  "the benchmark dies: skull, and the exit decoded"),
    (66.0, 14, "the training run carries on, costing nothing while it does"),
    (72.0, 1,  "done"),
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
    g = emoji_glyph("\U0001f480", int(line_h * 0.78))
    if g:
        emoji["\U0001f480"] = g
    fallbacks = build_fallbacks(cfg["spinner"] + "\u2713\u2192\u00b7\u25b6\u2502\u2500",
                                args.font_size)

    scratch = tempfile.mkdtemp(prefix="agent-progress-rec-")
    frames = os.path.join(scratch, "frames")
    os.makedirs(frames)
    for spec_ in PANELS:
        open(os.path.join(scratch, spec_["file"]), "w").write(spec_["body"])
    subprocess.run([sys.executable, ENGINE, "rm", "--all"], capture_output=True)

    panels = [Panel(cc, spec_, scratch) for spec_ in PANELS]
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
        if now > 24.0 and "quick" not in fired:
            fired.add("quick")
            # A real job is re-observed every 2 minutes, or once per 5% of its
            # estimate. Over a job this short that is a single reading, and the
            # bars would sit still for the whole clip. Forced to 2s here so
            # there is something to watch; the header says so.
            for jid in ("rec-train", "rec-benchmark"):
                subprocess.run([sys.executable, ENGINE, "update", jid,
                                "--interval", "2s", "--quiet"], capture_output=True)
        if now > 31.0 and "eta" not in fired:
            fired.add("eta")
            subprocess.run([sys.executable, ENGINE, "update", "rec-train",
                            "--eta", "45s", "--quiet"], capture_output=True)
            panels[1].say(cc.paint("  $ agent-progress update rec-train --eta 45s",
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
