"""Task-Manager style live chart of token throughput (tokens/min per provider), drawn on a Tk canvas.

Pure drawing: the data comes from live_tokens.LiveTokens.series(); colours are passed in by the dashboard."""

import bisect, math, os, tempfile
from datetime import datetime

NICE = (1, 2, 5, 10)
PLOT_L, PLOT_R = 38, 6  # left / right margin of the plot: the dashboard maps the pointer to the time axis with them
TICKS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 43200, 86400)  # tick spacings of a zoomed axis (s)
# Hatch patterns that tell the model bands of one provider apart (shades of one colour are too close). Rows of an 8x8
# tile, "#" = drawn. A family keeps its pattern, whatever else is in the chart; unknown families share the dots.
HATCH = {"solid": None,
         "rising": ["......##", ".....##.", "....##..", "...##...", "..##....", ".##.....", "##......", "#......#"],
         "falling": ["##......", ".##.....", "..##....", "...##...", "....##..", ".....##.", "......##", "#......#"],
         "level": ["........", "########", "########", "........", "........", "........", "........", "........"],
         "dots": ["##..##..", "##..##..", "........", "........", "..##..##", "..##..##", "........", "........"]}
FAMILY_HATCH = {"Sonnet": "solid", "Opus": "rising", "Haiku": "falling", "Fable": "level"}
_stipples = {}


def stipple(name):
    """Tk stipple argument of a HATCH pattern ("" for solid): Tk reads patterns from XBM files only, so each one is
    written to the temp dir once."""
    if name not in _stipples:
        rows = HATCH[name]
        if rows is None:
            _stipples[name] = ""
        else:
            path = os.path.join(tempfile.gettempdir(), f"pacer-hatch-{name}.xbm")
            data = ", ".join(hex(sum(1 << x for x, ch in enumerate(row) if ch == "#")) for row in rows)
            with open(path, "w", encoding="ascii") as f:
                f.write(f"#define h_width 8\n#define h_height {len(rows)}\nstatic char h_bits[] = {{\n{data}\n}};\n")  # Tk wants "{" as its own word
            _stipples[name] = "@" + path.replace("\\", "/")
    return _stipples[name]


def fmt_rate(v):
    v = abs(v)
    if v >= 1e6:
        return f"{v / 1e6:.1f}M"
    if v >= 1e3:
        return f"{v / 1e3:.0f}k" if v >= 1e4 else f"{v / 1e3:.1f}k"
    return f"{v:.0f}"


def nice_max(v, floor=10_000):
    """Smallest 1/2/5 x 10^k at or above v (never below floor): a steady y axis instead of a jumping one."""
    v = max(v, floor)
    mag = 10 ** (len(str(int(v))) - 1)
    for f in NICE:
        if f * mag >= v:
            return f * mag
    return 10 * mag


def mix(a, b, k):
    """Colour between hex colours a (k = 0) and b (k = 1)."""
    return "#" + "".join(f"{round(int(a[i:i + 2], 16) + (int(b[i:i + 2], 16) - int(a[i:i + 2], 16)) * k):02x}" for i in (1, 3, 5))


def draw_live(c, series, layout, pal, chips, active, resets=(), cache=None, split=False, hover=None):
    """series: {"t": [epochs], name: [tokens/min], "models": {name: {family: [tokens/min]}}} from LiveTokens.series (the
    shown window runs from the first t to the last t = `now`); layout: [(name, label, colour)]; pal: dict of
    fg/muted/grid/bg/tint colours; chips: keys of the preset chips (top right), active: the key shown as selected (or
    None). X is real time, so redrawing a moment later slides the picture left. resets: [(epoch, "5h"|"7d")] of usage-window
    resets, drawn as dashed vertical markers. cache: {name: text} shown in small type under that legend entry. split:
    every provider's area is stacked from one band per model family (shades of the provider colour, the legend lists
    the families). hover: an epoch under the pointer: a dashed line and dots on the curves at its nearest sample,
    whose time goes bold on the x axis and whose values the legend shows instead of the newest one.
    Returns [(x0, x1, key)]: click areas of the chips so the caller can switch to a preset."""
    c.delete("all")
    W, H = c.winfo_width(), c.winfo_height()
    if W < 100 or H < 60:
        return []
    times = series["t"]
    now = times[-1]
    start = times[0]
    window_s = now - start
    L, R, T, B = PLOT_L, PLOT_R, 40, 22  # T leaves room for the two legend rows (rate, cache hit)
    pw, ph = W - L - R, H - T - B
    yb = T + ph
    small = ("Segoe UI", 8)
    names = [n for n, _l, _c in layout]
    top = nice_max(max((max(series[n]) for n in names), default=0))

    def X(t):
        return L + pw * (t - start) / window_s

    def Y(v):
        return T + ph * (1 - min(v, top) / top)
    hi, hx = len(times) - 1, None  # the sample the legend and the dots show: the newest, or the one under the pointer
    if hover is not None:
        j = bisect.bisect_right(times, min(max(hover, start), now))
        hi = min((k for k in (j - 1, j) if 0 <= k < len(times)), key=lambda k: abs(times[k] - hover))
        hx = X(times[hi])

    for frac in (0, 0.5, 1):
        c.create_line(L, Y(top * frac), W - R, Y(top * frac), fill=pal["grid"])

    def ylabels():
        for frac in (0, 0.5, 1):
            c.create_text(L - 6, Y(top * frac), text=fmt_rate(top * frac), fill=pal["muted"], font=small, anchor="e", tags="ylabel")
    tick_s = next((s for s in TICKS if s / window_s * pw >= 60), TICKS[-1])  # the tick spacing follows the shown span
    fmt = "%H:%M" if tick_s >= 60 else "%H:%M:%S"
    off = datetime.fromtimestamp(now).astimezone().utcoffset().total_seconds()  # ticks at local clock multiples (4h ticks: 00, 04, 08 ...)
    k = math.floor((start + off) / tick_s) + 1  # vertical lines at fixed clock times: they slide with the data
    while k * tick_s - off < now:
        t = k * tick_s - off
        x = X(t)
        c.create_line(x, T, x, yb, fill=pal["grid"])
        if x - 22 > L and x + 22 < W - R and (hx is None or abs(x - hx) > 52):
            c.create_text(x, yb + 6, text=datetime.fromtimestamp(t).strftime(fmt), fill=pal["muted"], font=small, anchor="n")
        k += 1
    if hx is not None:  # the hovered sample's time instead of the tick labels it would run into
        c.create_text(min(max(hx, L + 26), W - R - 26), yb + 6, text=datetime.fromtimestamp(times[hi]).strftime(fmt),
                      fill=pal["fg"], font=("Segoe UI", 8, "bold"), anchor="n")

    def flat(values):
        return [q for t, y in zip(times, values) for q in (X(t), Y(y))]

    def area(values, fill, hatch=""):  # smooth like the line above it; doubled points keep the four corners sharp
        p = flat(values)
        c.create_polygon(p[0], yb, p[0], yb, *p[:2], *p, *p[-2:], p[-2], yb, p[-2], yb, fill=fill, outline="", smooth=True,
                         stipple=hatch)

    def curve(values, colour, width):
        c.create_line(*flat(values), fill=colour, width=width, joinstyle="round", capstyle="round", smooth=True)

    bands = {}  # name -> [(family, hatch, stacked top values, current rate)], bottom band first
    shade = {name: (mix(pal["bg"], colour, 0.16), mix(pal["bg"], colour, 0.62)) for name, _label, colour in layout}  # band fill, hatch lines
    for name, _label, colour in layout:
        fams = [(f, v) for f, v in series["models"][name].items() if max(v) > 0] if split else []
        acc = [0.0] * len(times)
        bands[name] = []
        for fam, values in fams:
            acc = [a + b for a, b in zip(acc, values)]
            bands[name].append((fam, stipple(FAMILY_HATCH.get(fam, "dots")), acc, values))
    for name, _label, colour in layout:  # areas first so no line is hidden by another series' fill
        if bands[name]:
            for _fam, hatch, acc, _vals in reversed(bands[name]):  # the upper band first, the lower ones paint over it
                area(acc, shade[name][0])
                if hatch:
                    area(acc, shade[name][1], hatch)
        elif max(series[name]) > 0:
            area(series[name], pal["tint"][name])
    for name, _label, colour in layout:
        for _fam, _hatch, acc, _vals in bands[name][:-1]:  # borders between the bands
            curve(acc, shade[name][1], 1)
        curve(series[name], colour, 2)
    for t, win in resets:  # usage-window resets: dashed marker + label, on top of the data
        if start < t <= now:
            x = X(t)
            col = pal["reset" + win]
            c.create_line(x, T, x, yb, fill=col, dash=(4, 3), width=1)
            right = x + 4 + 44 < W - R
            c.create_text(x + 4 if right else x - 4, T + 2, text=f"{win} reset", fill=col, font=small,
                          anchor="nw" if right else "ne")
    if hx is not None:  # the hovered sample: a dashed line through it and dots on the drawn curves
        c.create_line(hx, T, hx, yb, fill=pal["muted"], dash=(2, 3))
        for name, _label, colour in layout:
            if bands[name] or max(series[name]) > 0:  # the curves that are drawn
                y = Y(series[name][hi])
                c.create_oval(hx - 3, y - 3, hx + 3, y + 3, fill=colour, outline=pal["bg"])
    ylabels()


    cache = cache or {}  # name -> text shown under that legend entry
    rows = []  # one legend row of (colour, label, current rate, small text below, hatch or None); split: one entry per family
    for name, label, colour in layout:
        if split:  # one row for every provider too: the families side by side (narrow: fits() shortens them)
            rows[:] = [(rows[0] if rows else []) + [(colour, fam, vals[hi], None, (name, hatch))
                                                   for fam, hatch, _top, vals in reversed(bands[name])]
                       or [(colour, label, series[name][hi], None, None)]]
        else:
            rows[:] = [(rows[0] if rows else []) + [(colour, label, series[name][hi], cache.get(name), None)]]

    def legend(form):
        """Legend with the current rate (the newest sample, or the one under the pointer), in the form that fits:
        "Sonnet  12k/min" with the small text below, "Sonnet 12k", or the rate alone. Returns the right edge of every row."""
        edges = []
        for r, entries in enumerate(rows):
            x, y = L, 12 + 16 * r
            for colour, label, rate, below, hatch in entries:
                txt = (f"{label}  {fmt_rate(rate)}/min", f"{label} {fmt_rate(rate)}", fmt_rate(rate))[form]
                if hatch is None:
                    c.create_oval(x, y - 4, x + 8, y + 4, fill=colour, outline="", tags="legend")
                else:  # a swatch of the band: its fill and hatch
                    c.create_rectangle(x - 2, y - 6, x + 10, y + 6, fill=shade[hatch[0]][0], outline=colour, tags="legend")
                    if hatch[1]:
                        c.create_rectangle(x - 1, y - 5, x + 10, y + 6, fill=shade[hatch[0]][1], outline="", stipple=hatch[1], tags="legend")
                t = c.create_text(x + 13, y, text=txt, fill=pal["fg"], font=("Segoe UI", 9), anchor="w", tags="legend")
                right = c.bbox(t)[2]
                if below and form == 0:
                    b = c.create_text(x + 13, y + 16, text=below, fill=pal["muted"], font=("Segoe UI", 8), anchor="w", tags="legend")
                    right = max(right, c.bbox(b)[2])
                x = right + (16 if form == 0 else 10)
            edges.append(right if entries else L)
        return edges

    def fits(edges, chips_x):  # the first row shares its line with the range chips
        return all(e <= (chips_x if r == 0 else W - R) for r, e in enumerate(edges))
    edges = legend(0)
    hits, x = [], W - R  # range chips, right-aligned
    for key in reversed(chips):
        t = c.create_text(x - 6, 12, text=key, fill=pal["fg"] if key == active else pal["muted"],
                          font=("Segoe UI", 9, "bold" if key == active else "normal"), anchor="e")
        x0, _, x1, _ = c.bbox(t)
        if key == active:
            c.create_line(x0, 21, x1, 21, fill=pal["fg"])
        hits.append((x0 - 4, x1 + 4, key))
        x = x0 - 8
    for form in (1, 2):  # would run into the range chips (or out of the chart): shorten
        if not fits(edges, x):
            c.delete("legend")
            edges = legend(form)
    if all(max(series[n]) == 0 for n in names):
        c.create_text(L + pw / 2, T + ph / 2, text="No AI activity", fill=pal["muted"], font=("Segoe UI", 10))
    return hits
