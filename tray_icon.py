"""Pacer icons: a gauge on a colour tile, drawn pixel-exact (no antialiasing) so the small sizes stay crisp.

  tray icon    colour tile = pace of the 7d window (same verdict as the dashboard's 7d tab):
                 green = on plan, blue = using too little, orange = will hit the limit late in the window,
                 red = too much / will pause early, grey = no 7d data yet
               white arc  = usage of the limiting window (270 degree gauge, gap at the bottom)
  app icon     dark tile with the same gauge in teal-green (Pacer.exe, installer, Start menu)

Run this file to write a preview sheet: python tray_icon.py [out.png]
"""
import math
import time
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

GLOW_RGB = {"good": (46, 184, 92), "warn": (240, 160, 32), "bad": (225, 50, 50),
            "blue": (90, 155, 255), "muted": (141, 150, 163)}


NEEDLE_COLORS = {"too little": (70, 130, 255), "on plan": (46, 184, 92), "too much": (225, 50, 50),
                 "limit": (225, 50, 50), "early": (140, 140, 140)}
SPAN_S = {"5h": 5 * 3600, "7d": 7 * 86400}


# hitting the limit a little before the reset still counts as on plan: 4 h of the 7d window (2.4 %); the monthly Copilot quota gets the same share
EARLY_TOLERANCE_FRAC = {"7d": 4 * 3600 / (7 * 86400), "cop": 4 * 3600 / (7 * 86400)}


def pace(usage, reset_iso, window, threshold, now=None, span_s=None, early_s=None):
    """Pace of usage against the plan: where you are vs. where a steady burn to `threshold` would be.

    ratio = usage / (threshold * elapsed_fraction): 1.0 means you reach the pause point exactly at the
    reset, >1 you hit it early, <1 you stay below. Returns None without data, else a dict with
    verdict ('too little' <0.5, 'on plan' 0.5-1.0, 'too much' >1.0, 'limit' at/over threshold,
    'early' in the first 5% of the window where a rate means nothing), ratio, projected % at reset,
    minutes until the pause point at the current rate, minutes left in the window.
    """
    if usage is None or not threshold:
        return None
    if not reset_iso:  # no window running: nothing used yet -> needle all the way left
        if usage > 0:
            return None
        return {"verdict": "too little", "ratio": 0.0, "projected": 0.0, "eta_min": None, "left_min": None}
    try:
        left = datetime.fromisoformat(reset_iso).timestamp() - (now or time.time())
    except ValueError:
        return None
    span = span_s or SPAN_S[window]
    left = min(max(left, 0.0), float(span))
    elapsed = span - left
    frac = elapsed / span
    out = {"verdict": "early", "ratio": None, "projected": None, "eta_min": None, "left_min": left / 60}
    if usage >= threshold:
        out.update(verdict="limit", ratio=2.0, projected=usage / frac if frac else None)
        return out
    early_frac = 0.05 if early_s is None else early_s / span  # Claude windows: first 5%; monthly Copilot: first 6 h
    if frac < early_frac:  # a rate means little this early: needle shown anyway (grey), measured as if early_frac had elapsed
        out["ratio"] = usage / (threshold * early_frac)
        return out
    ratio = usage / (threshold * frac)
    out.update(ratio=ratio, projected=usage / frac)
    out["verdict"] = "too much" if ratio > 1.0 else "too little" if ratio < 0.5 else "on plan"
    if usage > 0:
        out["eta_min"] = (threshold - usage) / (usage / elapsed) / 60
        # tolerance: reaching the limit only a little before the reset still counts as on plan
        if out["verdict"] == "too much" and left - out["eta_min"] * 60 <= EARLY_TOLERANCE_FRAC.get(window, 0) * span:
            out["verdict"] = "on plan"
    return out


def needle_angle(ratio):
    """Dial angle in degrees from 12 o'clock: ratio 0 = -90 (left), 1 = 0 (top), >=2 = +90 (right)."""
    return max(-1.0, min(1.0, ratio - 1.0)) * 90.0


def window_verdict(p, usage, span_s):
    """Pace verdict of one window as a glow key (same thresholds as the dashboard tabs)."""
    if usage is None or p is None or p["left_min"] is None:
        return "muted"
    v = p["verdict"]
    if v == "limit":
        return "bad"
    if v == "early":
        return "muted"
    if v == "too much":
        early = p["left_min"] - p["eta_min"]
        return "warn" if early * 60 / span_s < 0.10 else "bad"
    if v == "too little":
        return "blue"
    return "good"


CRISP_MAX = 48  # icons up to this size get hard pixel edges
HOLE = 16      # supersampling for the masks; they are thresholded after scaling down, so edges stay hard
START, SWEEP = 135, 270  # gauge arc: starts bottom-left, runs clockwise over the top, 90 degree gap at the bottom


def _mask(size, draw):
    """Mask from `draw(ImageDraw, scale)` at HOLE x resolution, scaled down with a box filter. Up to CRISP_MAX px it is cut
    at 50 % (hard pixel edges instead of mushy greys); bigger icons keep the smooth edge."""
    big = Image.new("L", (size * HOLE, size * HOLE), 0)
    draw(ImageDraw.Draw(big), HOLE)
    small = big.resize((size, size), Image.BOX)
    return small.point(lambda v: 255 if v >= 128 else 0) if size <= CRISP_MAX else small


def _arc(d, c, r, w, a0, a1):
    """Thick arc with round caps (angles in degrees, 0 = 3 o'clock, clockwise)."""
    d.arc((c - r, c - r, c + r, c + r), a0, a1, fill=255, width=round(w))
    for a in (a0, a1):
        x, y = c + math.cos(math.radians(a)) * (r - w / 2), c + math.sin(math.radians(a)) * (r - w / 2)
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=255)


def _gauge_masks(size, usage):
    """(tile, track, fill) masks. The gauge sits on a whole-pixel grid: ring width and tile corner are rounded to pixels."""
    w = max(2, round(size * 0.20))
    margin = max(1, round(size * 0.07))
    r = size / 2 - margin - w / 2
    c = size / 2
    corner = max(2, round(size * 0.2))
    tile = _mask(size, lambda d, k: d.rounded_rectangle((0, 0, size * k - 1, size * k - 1), radius=corner * k, fill=255))
    track = _mask(size, lambda d, k: _arc(d, c * k, r * k, w * k, START, START + SWEEP))
    fill = _mask(size, lambda d, k: _arc(d, c * k, r * k, w * k, START, START + SWEEP * usage)) if usage > 0 else None
    return tile, track, fill


def _flat(size, tile, track, fill, c_tile, c_track, c_fill):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    for mask, col in ((tile, c_tile), (track, c_track), (fill, c_fill)):
        if mask is not None:
            img.paste(Image.new("RGBA", (size, size), col + (255,)), (0, 0), mask)
    return img


def make_icon(model, size=32):
    """Tray / window icon. model["glow"]: "good" | "warn" | "bad" | "blue" | "muted" (default), see window_verdict();
    model["pct"]: usage of the limiting window in percent (None = empty gauge)."""
    col = GLOW_RGB[model.get("glow", "muted")]
    usage = max(0.0, min(1.0, (model.get("pct") or 0) / 100))
    tile, track, fill = _gauge_masks(size, usage)
    return _flat(size, tile, track, fill, col, tuple(int(v * 0.5) for v in col), (255, 255, 255))


APP_TILE, APP_TRACK, APP_FILL, APP_DOT = (27, 31, 42), (62, 68, 84), (79, 209, 160), (236, 240, 246)


def make_app_icon(size=256):
    """App icon: dark tile, teal-green gauge (68 % shown), light centre dot."""
    tile, track, fill = _gauge_masks(size, 0.68)
    img = _flat(size, tile, track, fill, APP_TILE, APP_TRACK, APP_FILL)
    k = max(1, round(size * 0.07))
    dot = _mask(size, lambda d, s: d.ellipse(((size / 2 - k) * s, (size / 2 - k) * s, (size / 2 + k) * s, (size / 2 + k) * s), fill=255))
    img.paste(Image.new("RGBA", (size, size), APP_DOT + (255,)), (0, 0), dot)
    return img


def write_ico(path, sizes=(16, 24, 32, 48, 64, 128, 256)):
    """Windows .ico with one hand-drawn PNG frame per size (Pillow would only rescale the biggest one)."""
    import io, struct
    frames = []
    for n in sizes:
        buf = io.BytesIO()
        make_app_icon(n).save(buf, "PNG")
        frames.append((n, buf.getvalue()))
    out = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    for n, data in frames:
        out += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    with open(path, "wb") as f:
        f.write(out + b"".join(d for _n, d in frames))


def make_gauge(p, label, width=240):
    """Large speedometer dial for one window. p = result of pace() (or None)."""
    S = 3
    W, H = width * S, int(width * 0.80) * S
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy, R, T = W / 2, W * 0.56, W * 0.40, W * 0.10
    box = [cx - R, cy - R, cx + R, cy + R]
    for a0, a1, col in ((-90, -45, NEEDLE_COLORS["too little"]), (-45, 0, NEEDLE_COLORS["on plan"]),
                        (0, 90, NEEDLE_COLORS["too much"])):
        d.arc(box, a0 - 90, a1 - 90, fill=col, width=int(T))
    for a in (-90, -45, 0, 45, 90):  # ticks
        d.line([(cx + (R + 4) * math.sin(math.radians(a)), cy - (R + 4) * math.cos(math.radians(a))),
                (cx + (R - T - 4) * math.sin(math.radians(a)), cy - (R - T - 4) * math.cos(math.radians(a)))],
               fill=(230, 230, 230), width=int(2 * S))
    ratio = (p or {}).get("ratio")
    if ratio is not None:
        a = math.radians(needle_angle(ratio))
        tip = (cx + (R - T * 0.4) * math.sin(a), cy - (R - T * 0.4) * math.cos(a))
        d.line([(cx, cy), tip], fill=(16, 16, 16), width=int(9 * S))
        d.line([(cx, cy), tip], fill=(255, 255, 255), width=int(5 * S))
    d.ellipse([cx - 9 * S, cy - 9 * S, cx + 9 * S, cy + 9 * S], fill=(255, 255, 255), outline=(16, 16, 16), width=S * 2)
    try:
        font = ImageFont.truetype("segoeui.ttf", int(W * 0.062))
        big = ImageFont.truetype("segoeuib.ttf", int(W * 0.085))
    except Exception:
        font = big = ImageFont.load_default()
    d.text((cx - R, cy + W * 0.04), "too little", fill=(150, 190, 255), font=font, anchor="la")
    d.text((cx + R, cy + W * 0.04), "too much", fill=(255, 140, 140), font=font, anchor="ra")
    d.text((cx, W * 0.025), label, fill=(240, 240, 240), font=big, anchor="ma")
    return img.resize((width, H // S), Image.LANCZOS)


CASES = [
    ("7d good\non plan", dict(glow="good")),
    ("7d blue\ntoo little", dict(glow="blue")),
    ("7d orange\nlimit late", dict(glow="warn")),
    ("7d red\ntoo much", dict(glow="bad")),
    ("no 7d data", dict(glow="muted")),
]


def preview(path):
    from PIL import ImageFont
    base = dict(glow="muted")
    try:
        font = ImageFont.truetype("segoeui.ttf", 15)
    except Exception:
        font = ImageFont.load_default()
    cols, cw, ch = 4, 300, 190
    rows = math.ceil(len(CASES) / cols)
    sheet = Image.new("RGB", (cols * cw, rows * ch * 2), (255, 255, 255))
    for i, (label, m) in enumerate(CASES):
        m = {**base, **m}
        for band, bg, fg in ((0, (32, 32, 32), (230, 230, 230)), (1, (243, 243, 243), (30, 30, 30))):
            x, y = (i % cols) * cw, (i // cols) * ch + band * rows * ch
            dr = ImageDraw.Draw(sheet)
            dr.rectangle([x, y, x + cw, y + ch], fill=bg)
            big = make_icon(m, 128)
            sheet.paste(big, (x + 10, y + 10), big)
            for s, off in ((32, 150), (16, 195)):
                sm = make_icon(m, s)
                sheet.paste(sm, (x + off, y + 20), sm)
            dr.multiline_text((x + 10, y + 148), label, fill=fg, font=font)
    sheet.save(path)


if __name__ == "__main__":
    import sys
    preview(sys.argv[1] if len(sys.argv) > 1 else "tray_icon_preview.png")
