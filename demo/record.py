#!/usr/bin/env python3
"""Record a short .mov of agent-tqdm running real jobs.

Frames are rendered from live job state through the engine's own renderer, so
the video shows genuine output - not a mock-up. Encoding uses AVFoundation via
demo/mov_encoder.swift, so nothing beyond macOS and Pillow is required.

    python3 demo/record.py                      # -> demo/agent-tqdm.mov
    python3 demo/record.py --out /tmp/x.mov --fps 12
"""

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(os.path.dirname(HERE), "scripts", "agent_tqdm.py")

BG = (13, 17, 23)          # a calm terminal background
FG = (210, 210, 210)
PAD = 26
COLS, ROWS = 120, 8

# Menlo covers the block elements and box drawing the bars use, but not the
# Braille range the spinner comes from. Anything it lacks is drawn from a
# fallback that has it.
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
    """Split an ANSI-coloured string into (text, rgb) runs."""
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


def load_engine():
    spec = importlib.util.spec_from_file_location("agent_tqdm", ENGINE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bitmap(font, ch, box=48):
    img = Image.new("L", (box, box), 0)
    ImageDraw.Draw(img).text((4, 4), ch, font=font, fill=255)
    return img.tobytes()


def build_fallbacks(chars, size):
    """Pre-render each character the primary font cannot draw, as a grayscale
    mask scaled to the cell.

    Coverage is detected by rendering: a missing glyph comes out identical to a
    private-use codepoint (the .notdef box), which no real glyph matches. The
    substitute fonts are proportional and often draw these symbols small and
    high, so each one is rendered large, cropped to its ink, and scaled - that
    way it sits in the cell at the size the monospace text expects."""
    primary = ImageFont.truetype(PRIMARY_FONT, size)
    tofu, blank = _bitmap(primary, "\ue000"), _bitmap(primary, " ")
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
            if _bitmap(alt, ch) in (_bitmap(alt, "\ue000"), _bitmap(alt, " ")):
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
    """Apple Color Emoji only renders at fixed sizes, so draw big and shrink."""
    for size in (160, 96, 64, 48, 32, 20):
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
                        glyph = emoji[ch]
                        img.paste(glyph, (int(x), int(y + line_h * 0.08)), glyph)
                        x += cell_w * 2
                    else:
                        # a mask, tinted to the run's colour and centred in the cell
                        mask = fallbacks[ch]
                        tile = Image.new("RGB", mask.size, color)
                        img.paste(tile, (int(x + max(0, (cell_w - mask.width) / 2)),
                                         int(y + (line_h * 0.66 - mask.height) / 2)), mask)
                        x += cell_w
                else:
                    chunk += ch
            if chunk:
                draw.text((x, y), chunk, font=font, fill=color)
                x += len(chunk) * cell_w
        y += line_h
    return img


# Three jobs, each measured a different way, plus one that dies.
JOBS = [
    ("train", "--eta 10s --unit ep", """
import time
N = 24
for i in range(1, N + 1):
    time.sleep(0.72)
    print("Epoch %d/%d  loss %.3f" % (i, N, 2.4 / (i ** 0.5)), flush=True)
"""),
    ("convert", "--eta 20s --milestones 'decoding;resampling;encoding;verifying'", """
import time
for s in ["decoding", "resampling", "encoding", "verifying"]:
    print(s, flush=True)
    time.sleep(4.3)
"""),
    ("upload", "--eta 20s", """
import time
print("opening connection", flush=True)
time.sleep(3)
print("sending chunk 1", flush=True)
time.sleep(4)
raise ConnectionResetError("peer closed the connection")
"""),
]

CAPTIONS = [
    (0.0, "three jobs - a counter, named stages, and one that will fail"),
    (8.5, "upload died: skull, and the exit decoded, not a bare number"),
    (13.0, "train's 10s estimate was wrong - it is correcting itself"),
    (18.5, "done - two clean exits, and a crash that reported itself"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "agent-tqdm.mov"))
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument("--seconds", type=float, default=22.0)
    ap.add_argument("--font-size", type=int, default=22)
    args = ap.parse_args()

    cc = load_engine()
    cfg = cc.load_config()
    cfg["bar_width"] = 24

    font = ImageFont.truetype(PRIMARY_FONT, args.font_size)
    cell_w = font.getlength("M")
    line_h = int(args.font_size * 1.5)
    width = int(PAD * 2 + cell_w * COLS)
    height = PAD * 2 + line_h * ROWS
    size = (width + (width % 2), height + (height % 2))
    emoji = {}
    for ch in ("\U0001f480",):
        g = emoji_glyph(ch, int(line_h * 0.78))
        if g:
            emoji[ch] = g
    fallbacks = build_fallbacks(
        cfg["spinner"] + cfg["glyph_done"] + cfg["glyph_cancelled"]
        + cfg["glyph_stalled"] + "\u2192\u00b7", args.font_size)
    if fallbacks:
        print("fallback glyphs: %s" % " ".join(sorted(fallbacks)))

    scratch = tempfile.mkdtemp(prefix="agent-tqdm-rec-")
    frames = os.path.join(scratch, "frames")
    os.makedirs(frames)

    def cli(*a):
        return subprocess.run([sys.executable, ENGINE] + list(a),
                              capture_output=True, text=True).stdout

    for name, flags, src in JOBS:
        path = os.path.join(scratch, name + ".py")
        open(path, "w").write(src)
        subprocess.run("%s %s run --name rec-%s %s --interval 1s -- %s %s"
                       % (sys.executable, ENGINE, name, flags, sys.executable, path),
                       shell=True, capture_output=True)

    print("recording %.0fs at %dfps..." % (args.seconds, args.fps))
    start = time.time()
    n = 0
    while True:
        elapsed = time.time() - start
        if elapsed > args.seconds:
            break
        st = cc.state_ro()
        jobs = sorted((j for j in st["jobs"].values()
                       if (j.get("id") or "").startswith("rec-")),
                      key=lambda j: j.get("started") or 0)
        caption = ""
        for at, text in CAPTIONS:
            if elapsed >= at:
                caption = text
        lines = [cc.paint("agent-tqdm", "dim", True), ""]
        for j in jobs:
            lines.append("  " + cc.render_line(j, cfg, width=COLS - 4))
        while len(lines) < ROWS - 2:
            lines.append("")
        lines.append(cc.paint("  " + caption, "warn", True))
        make_frame(lines[:ROWS], font, cell_w, line_h, size, emoji, fallbacks).save(
            os.path.join(frames, "f%05d.png" % n))
        n += 1
        time.sleep(max(0.0, (n / float(args.fps)) - (time.time() - start)))

    print("rendered %d frames at %dx%d" % (n, size[0], size[1]))

    binary = os.path.join(scratch, "mov_encoder")
    subprocess.run(["swiftc", "-O", os.path.join(HERE, "mov_encoder.swift"),
                    "-o", binary], check=True)
    subprocess.run([binary, args.out, str(args.fps), frames], check=True)

    for name, _f, _s in JOBS:
        cli("rm", "rec-" + name)
    with cc.state_rw() as st:
        st["inbox"] = [e for e in st.get("inbox", [])
                       if not (e.get("job") or "").startswith("rec-")]
    shutil.rmtree(scratch, ignore_errors=True)
    mb = os.path.getsize(args.out) / 1e6
    print("%s  (%.2f MB)" % (args.out, mb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
