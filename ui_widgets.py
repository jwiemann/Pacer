"""Dark Tk widgets of the Pacer dashboard (button, stepper, Switch, Tab) and the colour palette they share.

The palette is plain hex strings. HEX maps the tone names dashboard_data returns ("good", "warn", ...) to them.
"""
import ctypes, os, sys
import tkinter as tk
import tkinter.font as tkfont

from PIL import Image, ImageDraw, ImageTk

BG, PANEL, FG, MUTED = "#1b1d21", "#24272d", "#e6e6e6", "#8d96a3"
INPUT, BTN, BTN_HOVER, BORDER = "#15171a", "#30343c", "#3d424c", "#3a3f48"
GOOD, WARN, BAD, BLUE = "#2eb85c", "#f0a020", "#e13232", "#5a9bff"
AREA, GRID, TAB_ON = "#2c374a", "#34383f", "#2c3340"
SPARK_BG = "#1f2227"  # plot field of the tab graphs: a step darker than PANEL, not black
VIOLET = "#b07cff"
HEX = {"good": GOOD, "warn": WARN, "bad": BAD, "blue": BLUE, "muted": MUTED}


FONT_FILES = ("JetBrainsMono-Regular.ttf", "JetBrainsMono-Bold.ttf")  # bundled in fonts/ (SIL OFL 1.1, see fonts/OFL.txt)
MONO_FAMILIES = ("JetBrains Mono", "JetBrainsMono NFM", "Cascadia Mono", "Consolas")  # the first one that exists is used
MONO = "Consolas"  # replaced by init_fonts()


def init_fonts():
    """Loads the bundled JetBrains Mono for this process only (nothing is installed: FR_PRIVATE) and picks the monospace
    family of the UI, the first of MONO_FAMILIES that exists. Needs a Tk root: call it before the first widget is built."""
    global MONO
    base = os.path.join(getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__)), "fonts")
    for name in FONT_FILES:
        try:
            ctypes.windll.gdi32.AddFontResourceExW(os.path.join(base, name), 0x10, 0)
        except (OSError, AttributeError):
            pass
    have = {f.lower() for f in tkfont.families()}
    MONO = next((f for f in MONO_FAMILIES if f.lower() in have), "Courier New")
    return MONO


def mono(size, bold=False):
    """Tk font tuple of the monospace UI font (numbers, headings, chart text)."""
    return (MONO, size, "bold") if bold else (MONO, size)


def corner_images(r, outer, fill, border):
    """The four corners (top left, top right, bottom left, bottom right) of a rounded box as r x r PIL images: the colour
    `outer` behind the box, a 1 px arc in `border`, the box `fill` inside; anti-aliased by 8x supersampling. Laid over the
    corners of a square widget whose own 1 px border is `border`, they round it."""
    S = 8
    n = 2 * r * S
    img = Image.new("RGB", (n, n), outer)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, n - 1, n - 1), radius=r * S, fill=border)
    if fill != border:
        d.rounded_rectangle((S, S, n - 1 - S, n - 1 - S), radius=(r - 1) * S, fill=fill)
    img = img.resize((2 * r, 2 * r), Image.LANCZOS)
    return [img.crop(box) for box in ((0, 0, r, r), (r, 0, 2 * r, r), (0, r, r, 2 * r), (r, r, 2 * r, 2 * r))]


_corners = {}  # (r, outer, fill, border) -> the four PhotoImages (kept alive here)


def round_corners(widget, r, outer, fill, border, which=(True, True, True, True)):
    """Rounds the corners of a square Frame-like widget (Tk cannot clip widgets): small corner images are placed over its
    corners. `outer` = colour behind the widget; `which` picks the corners (top left, top right, bottom left, bottom
    right) that are rounded, e.g. a combo button keeps its joint square. Returns repaint(fill=None, border=None,
    outer=None) for widgets that change colour (hover, selected)."""
    state = {"outer": outer, "fill": fill, "border": border}
    labels = []
    places = (dict(x=0, y=0, anchor="nw"), dict(relx=1.0, x=0, y=0, anchor="ne"),
              dict(x=0, rely=1.0, y=0, anchor="sw"), dict(relx=1.0, rely=1.0, x=0, y=0, anchor="se"))
    for i, kw in enumerate(places):
        if which[i]:
            lb = tk.Label(widget, bd=0, highlightthickness=0, padx=0, pady=0)
            lb.place(bordermode="outside", **kw)
            labels.append((i, lb))

    def paint():
        key = (r, state["outer"], state["fill"], state["border"])
        if key not in _corners:
            _corners[key] = [ImageTk.PhotoImage(im) for im in corner_images(*key)]
        for i, lb in labels:
            lb.config(image=_corners[key][i], bg=state["outer"])

    def repaint(fill=None, border=None, outer=None):
        new = {"fill": fill or state["fill"], "border": border or state["border"], "outer": outer or state["outer"]}
        if new != state:
            state.update(new)
            paint()
    paint()
    return repaint


def button(parent, text, cmd, primary=False, icon=None, pad=(12, 7), corners=(True, True, True, True)):
    """Flat dark button with hover state and optional Segoe MDL2 icon (tk.Button ignores the dark theme).
    set_icon_only(True) hides the text (narrow window); `label()` is the text it would show, for a tooltip."""
    base, hover = ("#2f6fe0", "#4580f0") if primary else (BTN, BTN_HOVER)
    fg = "#ffffff" if primary else FG
    f = tk.Frame(parent, bg=base, cursor="hand2")
    inner = tk.Frame(f, bg=base)
    inner.pack(padx=pad[0], pady=pad[1], expand=True)  # centred when the button is stretched to the row height
    parts = [inner]
    if icon:
        parts.append(tk.Label(inner, text=icon, bg=base, fg=fg, font=("Segoe MDL2 Assets", 10)))
        parts[-1].pack(side="left", padx=(0, 8))
    parts.append(tk.Label(inner, text=text, bg=base, fg=fg, font=mono(9)))
    parts[-1].pack(side="left")
    allw = [f] + parts
    rc = round_corners(f, 6, parent.cget("bg"), base, base, corners)  # rounded; the corners follow the hover colour

    col = {"base": base, "hover": hover}

    def paint(c):
        for w in allw:
            w.config(bg=c)
        rc(fill=c, border=c)

    def set_state(label=None, danger=False):
        """Swap the label and/or turn the button red (used for 'click again to confirm')."""
        col["base"], col["hover"] = ("#c93c3c", "#e04a4a") if danger else (base, hover)
        parts[-1].config(text=label or text)
        for w in parts[1:]:
            w.config(fg="#ffffff" if danger else fg)
        paint(col["base"])
    f.set_state = set_state

    def set_icon_only(on):
        if not icon:
            return
        if on:
            parts[-1].pack_forget()
        else:
            parts[-1].pack(side="left")
        parts[1].pack_configure(padx=0 if on else (0, 8))
    f.set_icon_only = set_icon_only
    f.label = lambda: parts[-1].cget("text")
    f.parts = allw
    for w in allw:
        w.bind("<Button-1>", lambda _e: cmd())
        w.bind("<Enter>", lambda _e: paint(col["hover"]))
        w.bind("<Leave>", lambda _e: paint(col["base"]))
    return f


def split_button(parent, text, cmd, on_menu, icon=None):
    """Combo button: the main part runs `cmd`, the narrow arrow part on its right runs `on_menu` (opens a menu). Offers the
    same interface as button(): set_state, set_icon_only (main part only), label, parts."""
    f = tk.Frame(parent, bg=BG)
    main = button(f, text, cmd, icon=icon, corners=(True, False, True, False))  # only the outer corners are rounded: the joint stays square
    main.pack(side="left", fill="both", expand=True)
    tk.Frame(f, bg=BG, width=1).pack(side="left", fill="y")
    arrow = button(f, "\u25be", on_menu, pad=(9, 7), corners=(False, True, False, True))
    arrow.pack(side="left", fill="y")
    f.set_state, f.set_icon_only, f.label = main.set_state, main.set_icon_only, main.label
    f.parts = main.parts + arrow.parts
    return f


def stepper(parent, var, on_edit, lo=1, hi=100):
    """Dark number input: [-] [value] [+] (tk.Spinbox cannot be themed or padded)."""
    f = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
    entry = tk.Entry(f, textvariable=var, width=4, justify="center", bg=INPUT, fg=FG, insertbackground=FG,
                     relief="flat", bd=0, highlightthickness=0, font=mono(10),
                     selectbackground=BLUE, selectforeground="#ffffff")

    def step(d):
        try:
            v = int(var.get())
        except ValueError:
            v = lo
        var.set(str(min(hi, max(lo, v + d))))
        on_edit()

    def btn(text, d):
        b = tk.Label(f, text=text, width=3, bg=BTN, fg=FG, font=mono(10), cursor="hand2")
        b.bind("<Button-1>", lambda _e: step(d))
        b.bind("<Enter>", lambda _e: b.config(bg=BTN_HOVER))
        b.bind("<Leave>", lambda _e: b.config(bg=BTN))
        return b
    btn("\u2212", -1).pack(side="left", fill="y")
    entry.pack(side="left", ipady=4, padx=6)
    btn("+", 1).pack(side="left", fill="y")
    entry.bind("<KeyPress>", lambda _e: on_edit())
    entry.bind("<Up>", lambda _e: step(1))
    entry.bind("<Down>", lambda _e: step(-1))
    return f


class Switch(tk.Frame):
    """Dark on/off switch with its label on the left, the knob on the right (tk.Checkbutton cannot be themed). Bound to a
    BooleanVar; `command` runs after a click; configure(state="disabled") greys it out."""
    W, H = 30, 16

    def __init__(self, parent, text, var, command):
        super().__init__(parent, bg=PANEL, cursor="hand2")
        self.var, self.command, self.enabled = var, command, True
        self.lbl = tk.Label(self, text=text, bg=PANEL, fg=FG, font=("Segoe UI", 9), anchor="w", cursor="hand2")
        self.cv = tk.Canvas(self, width=self.W, height=self.H, bg=PANEL, highlightthickness=0, cursor="hand2")
        self.lbl.pack(side="left", fill="x", expand=True)
        self.cv.pack(side="right")
        for w in (self, self.lbl, self.cv):
            tk.Misc.bind(w, "<Button-1>", lambda _e: self.click())
        self.pos, self._anim, self.img = None, None, None
        var.trace_add("write", lambda *_a: self.draw())
        self.draw()

    def click(self):
        if self.enabled:
            self.var.set(not self.var.get())
            self.command()

    def draw(self):
        """Move the knob to the variable's state (animated once shown) and grey the label when disabled."""
        self.lbl.config(fg=FG if self.enabled else MUTED)
        goal = 1.0 if self.var.get() else 0.0
        if self.pos is None:
            self.pos = goal
            self.paint()
        elif self.pos != goal and self._anim is None:
            self.step()
        elif self._anim is None:
            self.paint()

    def step(self):
        goal = 1.0 if self.var.get() else 0.0  # read each frame: a click during the animation just reverses it
        self.pos = goal if abs(goal - self.pos) <= 0.25 else self.pos + (0.25 if goal > self.pos else -0.25)
        self.paint()
        self._anim = None if self.pos == goal else self.after(16, self.step)

    def paint(self):
        """Supersampled (4x) render: tk canvas shapes are not antialiased, PIL's are."""
        S, W, H, t = 4, self.W, self.H, self.pos
        on = (0x5d, 0x66, 0x74) if self.enabled else (0x35, 0x39, 0x41)
        off = (0x2c, 0x30, 0x37) if self.enabled else (0x26, 0x29, 0x2f)
        mix = lambda a, b, k: tuple(round(x + (y - x) * k) for x, y in zip(a, b))
        img = Image.new("RGB", (W * S, H * S), (0x24, 0x27, 0x2d))  # PANEL
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, W * S - 1, H * S - 1), radius=H * S // 2, fill=mix(off, on, t),
                            outline=mix((0x3d, 0x42, 0x4c), (0x70, 0x7a, 0x89), t) if self.enabled else (0x2f, 0x33, 0x3a), width=S)
        r, m = 4.5 * S, H * S / 2
        cx = m + (W * S - 2 * m) * t
        knob = mix((0x7b, 0x83, 0x91), (0xee, 0xf0, 0xf3), t) if self.enabled else (0x4a, 0x50, 0x5a)
        d.ellipse((cx - r, m - r, cx + r, m + r), fill=knob)
        self.img = ImageTk.PhotoImage(img.resize((W, H), Image.LANCZOS))
        self.cv.delete("all")
        self.cv.create_image(0, 0, image=self.img, anchor="nw")

    def configure(self, cnf=None, **kw):
        if "state" in kw:
            self.enabled = kw.pop("state") != "disabled"
            self.draw()
        return super().configure(cnf, **kw) if (cnf or kw) else None
    config = configure

    def bind(self, seq=None, func=None, add=None):
        for w in (self, self.lbl, self.cv):
            tk.Misc.bind(w, seq, func, add=True)


class Tab:
    """Window selector: state dot, window name, mini graph, usage. The selected tab gets a blue frame.
    layout(compact=True) turns it into a small tile (name above the usage, no graph) for a narrow window."""
    SPARK_MIN_W, SPARK_H = 84, 40

    def __init__(self, parent, name, on_click, on_right_click=None, on_double_click=None):
        self.frame = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        self.inner = tk.Frame(self.frame, padx=4, pady=2)
        self.inner.pack(fill="both", expand=True)
        self.dot = tk.Label(self.inner, text="\u25cf", font=("Segoe UI", 9), padx=0, pady=0)
        self.name = tk.Label(self.inner, text=name, font=mono(9, True), width=6, anchor="w", padx=0, pady=0)  # same width in every tab: equal graphs
        self.use = tk.Label(self.inner, text="-", font=mono(9), width=6, anchor="e", padx=0, pady=0)
        self.spark = tk.Canvas(self.inner, width=self.SPARK_MIN_W, height=self.SPARK_H, highlightthickness=0)
        self.layout(False)
        self.round = round_corners(self.frame, 6, parent.cget("bg"), PANEL, BORDER)  # recoloured in set()
        self.parts = (self.inner, self.dot, self.name, self.use, self.spark)
        self.spark_state = (None, MUTED)  # what _draw_spark last got, to redraw when the canvas is resized
        self.spark.bind("<Configure>", lambda _e: self._draw_spark(*self.spark_state))
        for w in (self.frame,) + self.parts:
            w.config(cursor="hand2")
            w.bind("<Button-1>", lambda _e: on_click())
            if on_double_click:  # the second click of a double click runs this instead of on_click
                w.bind("<Double-Button-1>", lambda _e: on_double_click())
            if on_right_click:
                w.bind("<Button-3>", lambda _e: on_right_click())

    def layout(self, compact):
        for w in (self.dot, self.name, self.use, self.spark):
            w.grid_forget()
        self.inner.columnconfigure(1, weight=1 if compact else 0)
        self.inner.columnconfigure(2, weight=0 if compact else 1)
        if compact:
            self.inner.config(padx=4, pady=1)
            self.dot.config(font=("Segoe UI", 7))
            self.name.config(width=0, font=mono(8, True))
            self.use.config(width=5, anchor="w", font=mono(10, True))
            self.dot.grid(row=0, column=0)
            self.name.grid(row=0, column=1, sticky="w", padx=(2, 0))
            self.use.grid(row=1, column=0, columnspan=2, sticky="w")
        else:
            self.inner.config(padx=0, pady=0)  # the graph touches the tab's border
            self.dot.config(font=("Segoe UI", 9))
            self.name.config(width=6, font=mono(9, True))  # same width in every tab: equal graphs
            self.use.config(width=6, anchor="e", font=mono(9))
            self.dot.grid(row=0, column=0, padx=(4, 0))
            self.name.grid(row=0, column=1, padx=(3, 3))
            self.spark.grid(row=0, column=2, sticky="nsew")  # takes the width left and the whole tab height
            self.use.grid(row=0, column=3, padx=(3, 4))

    def set(self, usage, color, selected, spark=None, paused=False):
        """spark: (points, top) with points [(x 0..1, value)] drawn like a Task Manager graph (value 0..top), or None.
        paused: the provider behind this window is paused or draining: a pause sign replaces the state dot."""
        self.frame.config(bg=BLUE if selected else BORDER)
        self.round(fill=TAB_ON if selected else PANEL, border=BLUE if selected else BORDER)
        for w in self.parts:
            w.config(bg=SPARK_BG if w is self.spark else TAB_ON if selected else PANEL)  # the graph is a slightly darker field: it shows where the window starts and ends
        self.dot.config(fg=color, text="\u23f8" if paused else "\u25cf")
        self.name.config(fg=FG if selected else MUTED)
        self.use.config(text=usage, fg=FG if selected else MUTED)
        self._draw_spark(spark, color)

    def _draw_spark(self, spark, color):
        self.spark_state = (spark, color)
        c = self.spark
        w, h = c.winfo_width(), c.winfo_height()
        c.delete("all")
        if w < 10:  # not laid out yet; <Configure> redraws
            return
        if not spark or len(spark[0]) < 2 or spark[1] <= 0:
            return
        pts, top = spark
        # no outline of its own: the tab's 1 px border is the frame, so the graph runs edge to edge. Tk leaves out the last
        # pixel column / row of a polygon and of a line's end: run one pixel further
        xy = [(x * w, (h - 1) - min(max(v / top, 0), 1) * (h - 1)) for x, v in pts]
        flat = [q for p in xy for q in p]
        c.create_polygon(xy[0][0], h, *flat, xy[-1][0], h, fill=AREA, outline="")
        c.create_line(*flat, fill=color)


