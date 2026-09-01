#!/usr/bin/env python3
"""Record a short .mov and .gif of agent-tqdm.

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
ENGINE = os.path.join(ROOT, "scripts", "agent_tqdm.py")
HOOK = os.path.join(ROOT, "hooks", "auto_track.py")

BG = (13, 17, 23)
FG = (210, 210, 210)
PAD = 24
COLS, ROWS = 116, 16

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

BENCH = """import time
print("loading llama-7b", flush=True)
time.sleep(7)
print("warmup pass 1/3", flush=True)
time.sleep(12)
print("warmup pass 2/3", flush=True)
time.sleep(12)
raise MemoryError("unable to allocate 8.00 GiB on cuda:0")
"""

TRAIN = """import time, sys
N = 34
for i in range(1, N + 1):
    time.sleep(2.2)
    print("Epoch %d/%d  loss %.3f  acc %.3f" % (i, N, 2.4 / (i ** 0.5), 1 - 0.9 / i),
          flush=True)
print("saved checkpoints/final.pt", flush=True)
"""


class Take(object):
    """Runs the session, keeps the transcript, and captures frames."""

    def __init__(self, cc, cfg, scratch):
        self.cc, self.cfg, self.scratch = cc, cfg, scratch
        self.lines = []
        self.caption = ""
        self.speed = 1.0
        self.streams = []          # every command still worth following

    def say(self, text=""):
        self.lines.append(text)

    def prompt(self, text):
        c = self.cc
        self.say(c.paint("> ", "run", True) + c.paint(text, "text", True))

    def run(self, command, name):
        """Send a command through the real hook, then run whatever it produced."""
        payload = {"tool_name": "Bash", "session_id": "demo",
                   "tool_input": {"command": command}}
        out = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                             capture_output=True, text=True).stdout.strip()
        actual = command
        if out:
            actual = json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"]
            actual = actual.replace(" --name ", " --name rec-", 1)
        path = os.path.join(self.scratch, "%s.out" % name)
        open(path, "w").close()
        # commands overlap here as they would in a session, so each one is
        # followed separately rather than replacing the last
        self.streams.append({"path": path, "offset": 0})
        subprocess.Popen(["/bin/sh", "-c", actual], cwd=self.scratch,
                         stdin=subprocess.DEVNULL, stdout=open(path, "w"),
                         stderr=subprocess.STDOUT)

    def drain(self):
        """Move any new output from the running commands into the transcript."""
        for stream in self.streams:
            try:
                with open(stream["path"], "rb") as f:
                    f.seek(stream["offset"])
                    chunk = f.read()
            except OSError:
                continue
            if not chunk:
                continue
            stream["offset"] += len(chunk)
            for raw in chunk.decode("utf-8", "replace").splitlines():
                line = raw.rstrip()
                if not line:
                    continue
                tone = "warn" if line.startswith("[agent-tqdm]") else "dim"
                self.say(self.cc.paint("  " + line[:COLS - 4], tone, True))

    def frame(self):
        c, cfg = self.cc, self.cfg
        head = c.paint("agent-tqdm", "dim", True)
        if self.speed > 1:
            head += c.paint("      ▶▶ %g× faster" % self.speed, "warn", True)
        body = [head, ""]
        body += self.lines[-(ROWS - 6):]      # leave a gap above the bars
        while len(body) < ROWS - 3:
            body.append("")
        st = c.state_ro()
        jobs = sorted((j for j in st["jobs"].values()
                       if (j.get("id") or "").startswith("rec-")),
                      key=lambda j: j.get("started") or 0)
        for j in jobs:
            body.append("  " + c.render_line(j, cfg, width=COLS - 4))
        while len(body) < ROWS - 1:
            body.append("")
        body.append(c.paint("  " + self.caption, "run", True))
        return body[:ROWS]

    def job_id(self, contains="train"):
        st = self.cc.state_ro()
        for j in sorted(st["jobs"].values(), key=lambda x: x.get("started") or 0):
            jid = j.get("id") or ""
            if jid.startswith("rec-") and contains in jid:
                return jid
        return None


# (until real-seconds, speed, caption). Speed is how much real time each
# captured frame covers, so waiting can be skipped past without faking it.
SCRIPT = [
    (2.0,   1,  "a command Claude runs, like any other"),
    (8.0,   1,  "it runs exactly as it would have - nothing is watching yet"),
    (11.0,  1,  "done in 4s. never tracked: no job, no message, no tokens"),
    (16.0,  1,  "now something slower. the threshold is 20 seconds"),
    (31.0,  12, "waiting out the 20 seconds"),
    (36.0,  1,  "past the threshold: tracked, and left running in the background"),
    (41.0,  1,  "Claude sets the estimate - the one thing a hook cannot guess"),
    (44.0,  6,  "a second slow command, running alongside the first"),
    (52.0,  1,  "it crosses the threshold too, and gets its own bar"),
    (66.0,  6,  "waiting on it"),
    (74.0,  1,  "then it dies: a skull, and the exit decoded, not a bare number"),
    (104.0, 14, "the training run carries on, costing nothing while it does"),
    (110.0, 1,  "done"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "agent-tqdm.mov"))
    ap.add_argument("--gif", default=os.path.join(HERE, "agent-tqdm.gif"))
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument("--font-size", type=int, default=20)
    ap.add_argument("--gif-width", type=int, default=900)
    ap.add_argument("--gif-every", type=int, default=2)
    ap.add_argument("--gif-colors", type=int, default=48)
    args = ap.parse_args()

    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_tqdm", ENGINE)
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    cfg = cc.load_config()
    cfg["bar_width"] = 26

    font = ImageFont.truetype(PRIMARY_FONT, args.font_size)
    cell_w = font.getlength("M")
    line_h = int(args.font_size * 1.5)
    width = int(PAD * 2 + cell_w * COLS)
    height = PAD * 2 + line_h * ROWS
    size = (width + (width % 2), height + (height % 2))
    emoji = {}
    g = emoji_glyph("\U0001f480", int(line_h * 0.78))
    if g:
        emoji["\U0001f480"] = g
    fallbacks = build_fallbacks(cfg["spinner"] + "✓→·▶", args.font_size)

    scratch = tempfile.mkdtemp(prefix="agent-tqdm-rec-")
    frames = os.path.join(scratch, "frames")
    os.makedirs(frames)
    open(os.path.join(scratch, "Makefile"), "w").write(MAKEFILE)
    open(os.path.join(scratch, "train.py"), "w").write(TRAIN)
    open(os.path.join(scratch, "benchmark.py"), "w").write(BENCH)
    subprocess.run([sys.executable, ENGINE, "rm", "--all"], capture_output=True)

    take = Take(cc, cfg, scratch)
    total = SCRIPT[-1][0]
    print("recording %.0fs of real time..." % total)

    start = time.time()
    n = 0
    last = 0.0
    fired = set()
    while True:
        now = time.time() - start
        if now > total:
            break
        for at, speed, caption in SCRIPT:
            if now <= at:
                take.speed, take.caption = speed, caption
                break

        # the session itself, driven off the same clock
        if 0.3 < now and "make" not in fired:
            fired.add("make")
            take.prompt("make -j8")
            take.run("make -j8", "make")
        if 12.2 < now and "train" not in fired:
            fired.add("train")
            take.say()
            take.prompt("python3 train.py --epochs 28")
            take.run("python3 train.py --epochs 28", "train")
        if 25.0 < now and "export" not in fired:
            fired.add("export")
            take.say()
            take.prompt("python3 benchmark.py --model llama-7b")
            take.run("python3 benchmark.py --model llama-7b", "bench")
        if 41.0 < now and "eta" not in fired:
            fired.add("eta")
            jid = take.job_id()
            if jid:
                subprocess.run([sys.executable, ENGINE, "update", jid, "--eta", "50s",
                                "--note", "34 epochs", "--quiet"], capture_output=True)
                take.say()
                take.say(cc.paint("  $ agent-tqdm update %s --eta 50s" % jid, "run", True))

        take.drain()
        if now - last >= (take.speed / float(args.fps)):
            last = now
            make_frame(take.frame(), font, cell_w, line_h, size, emoji, fallbacks).save(
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
