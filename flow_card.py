"""The Status card of the dashboard: one lane per AI provider, read from left to right.

    status of the provider  ->  governor rule check  ->  the switch of its agents in Paperclip

Thin lines connect the three stages; small balls run along them in that direction (ui_widgets.Link). The services sit
where they belong in that flow: the governor above the rule checks, Paperclip with its server and watchdog above the
agent switches. The Dashboard owns the data; this class owns the widgets, their layout and the animation.
"""
import tkinter as tk

from ui_widgets import PANEL, FG, MUTED, BORDER, Switch, Link, button, mono, round_corners

GAP = 12
# provider -> (section title, its rows, the rows whose reading feeds the rule check)
LANES = {
    "claude": ("CLAUDE", ("Claude login", "Throughput 5h", "Throughput 7d", "Resets 5h", "Resets 7d"), ("Throughput 5h", "Throughput 7d")),
    "copilot": ("COPILOT", ("Copilot login", "Copilot usage", "Copilot resets"), ("Copilot usage",)),
    "openrouter": ("OPENROUTER", ("OpenRouter key", "OpenRouter usage", "OpenRouter resets"), ("OpenRouter usage",)),
}
COLS = 6  # label | value | line | rule check | line | agent switch


class FlowCard:
    def __init__(self, dash, card, on_toggle, on_update):
        """`card`: the bordered frame to fill. The row widgets are registered in dash.lbls / dash.vals / dash.heads under
        their names, so Dashboard.put() and the tooltips work on them like on any Status row."""
        self.dash, self.card = dash, card
        self.lanes, self.compact, self.pc_on = {}, False, True
        self.sig = None  # geometry the line starts were computed for
        self.status = tk.Label(card, text="STATUS", bg=PANEL, fg=MUTED, font=mono(8, True))
        self.rule_line = tk.Frame(card, bg=BORDER, height=1)

        self.gov = tk.Frame(card, bg=PANEL)  # the governor: the component that checks the rules
        tk.Label(self.gov, text="GOVERNOR", bg=PANEL, fg=MUTED, font=mono(8, True)).pack(side="left")
        dash.vals["Governor"] = tk.Label(self.gov, bg=PANEL, fg=FG, font=mono(9), anchor="w")
        dash.vals["Governor"].pack(side="left", padx=(8, 0))

        self.pc = tk.Frame(card, bg=PANEL)  # Paperclip: where the agents live; its server and watchdog keep it running
        tk.Label(self.pc, text="PAPERCLIP", bg=PANEL, fg=MUTED, font=mono(8, True)).grid(row=0, column=0, sticky="w")
        self.pc_frame = tk.Frame(self.pc, bg=PANEL)  # status text, or while an update is offered the update button
        self.pc_frame.grid(row=0, column=1, sticky="e", padx=(8, 0))
        dash.vals["Paperclip"] = tk.Label(self.pc_frame, bg=PANEL, fg=FG, font=mono(9), anchor="w")
        dash.vals["Paperclip"].grid(row=0, column=0, sticky="w")
        self.upd_btn = button(self.pc_frame, "Update Paperclip", on_update, pad=(10, 2))
        self.upd_btn.grid(row=0, column=0, sticky="w")
        self.upd_btn.grid_remove()
        sub = tk.Frame(self.pc, bg=PANEL)
        sub.grid(row=1, column=0, columnspan=2, sticky="e", pady=(3, 0))
        for key in ("Server", "Watchdog"):
            tk.Label(sub, text=key, bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(10, 0))
            dash.vals[key] = tk.Label(sub, bg=PANEL, fg=FG, font=mono(9), anchor="w")
            dash.vals[key].pack(side="left", padx=(6, 0))
        self.pc_sub = sub

        row = 2
        for name, (title, items, feeders) in LANES.items():
            lane = self.lanes[name] = {"top": row, "n": len(items), "static": []}
            head = dash.heads[title] = tk.Label(card, text=title, bg=PANEL, fg=MUTED, font=mono(8, True))
            head.sep = None if row == 2 else tk.Frame(card, bg=BORDER, height=1)  # divider above every lane but the first
            head.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0 if row == 2 else 14, 0))
            if head.sep:
                head.sep.grid(row=row, column=0, columnspan=COLS, sticky="new", pady=(5, 0))
            lane["static"] += [head] + ([head.sep] if head.sep else [])
            for r, key in enumerate(items, row + 1):
                dash.lbls[key] = tk.Label(card, text=key, bg=PANEL, fg=MUTED, font=("Segoe UI", 9))
                dash.lbls[key].grid(row=r, column=0, sticky="w", pady=2)
                dash.vals[key] = tk.Label(card, bg=PANEL, fg=FG, font=mono(9), anchor="w")
                dash.vals[key].grid(row=r, column=1, sticky="w", padx=(GAP, 0), pady=2)
                lane["static"] += [dash.lbls[key], dash.vals[key]]
            lane["rule"], lane["rule_lines"] = self.node(), []
            lane["rule_tip"] = ""
            for i, (font, fg) in enumerate(((mono(9, True), FG), (mono(9), FG), (("Segoe UI", 8), MUTED), (("Segoe UI", 8), MUTED))):
                lb = tk.Label(lane["rule"], bg=PANEL, fg=fg, font=font, anchor="w")
                lb.grid(row=i, column=0, sticky="w")
                lane["rule_lines"].append(lb)
            lane["toggle_box"], lane["var"] = self.node(), tk.BooleanVar(value=False)
            lane["switch"] = Switch(lane["toggle_box"], "", lane["var"], lambda n=name: on_toggle(n))
            lane["switch"].pack(fill="x")
            lane["tip"] = ""
            lane["link_in"], lane["link_out"] = Link(card, from_text=True), Link(card)
            lane["link_in"].connect([dash.vals[k] for k in feeders], lane["rule"])
            lane["link_out"].connect([lane["rule"]], lane["toggle_box"])
            for box, key, parts in ((lane["rule"], "rule_tip", lane["rule"].winfo_children()), (lane["toggle_box"], "tip", [lane["switch"]])):
                for t in [box] + list(parts):  # Switch.bind covers the label and the knob
                    t.bind("<Enter>", lambda e, n=name, k=key: dash.show_tip(e, self.lanes[n][k]), add="+")
                    t.bind("<Leave>", dash.hide_tip, add="+")
            row += len(items) + 3  # title, rows, and two spare rows for the compact layout
        self.last = row

    def node(self):
        """A bordered, rounded box of the flow: the rule check and the agent switch."""
        box = tk.Frame(self.card, bg=PANEL, highlightbackground=BORDER, highlightthickness=1, padx=10, pady=6)
        round_corners(box, 6, PANEL, PANEL, BORDER)
        return box

    # ---- layout
    def place(self, compact, pc_on, shown):
        """Grid everything for the layout: wide = the three stages side by side with lines between them; compact = each
        lane's rule check and switch below its rows, no lines. Lanes of providers not in use, and everything that
        belongs to Paperclip without it, are hidden. `shown`: provider -> in use."""
        self.compact, self.pc_on = compact, pc_on
        c = self.card
        flow = pc_on and not compact  # the three stages side by side: connectors and both nodes take their width
        for col, size in ((2, 28), (3, 120), (4, 28), (5, 110)):
            c.columnconfigure(col, minsize=size if flow else 0, weight=1 if flow and col in (2, 4) else 0)
        self.status.grid(row=0, column=0, columnspan=2, sticky="nw")
        self.rule_line.grid(row=1, column=0, columnspan=COLS, sticky="ew", pady=(6, 4))
        if compact:
            self.gov.grid(row=self.last, column=0, columnspan=2, sticky="w", pady=(14, 0))
            self.pc.grid(row=self.last + 1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        else:
            self.gov.grid(row=0, column=3, columnspan=2, sticky="nw", pady=0)
            self.pc.grid(row=0, column=4, columnspan=2, sticky="ne", pady=0)
        if not pc_on:
            self.pc.grid_remove()
        for name, lane in self.lanes.items():
            on = shown[name]
            for w in lane["static"]:
                w.grid() if on else w.grid_remove()
            flow = on and pc_on
            top, n = lane["top"] + 1, lane["n"]
            for w in (lane["link_in"], lane["link_out"], lane["rule"], lane["toggle_box"]):
                w.grid_remove()
            if not flow:
                continue
            if compact:
                lane["rule"].grid(row=top + n, column=0, columnspan=COLS, rowspan=1, sticky="ew", pady=(8, 3))
                lane["toggle_box"].grid(row=top + n + 1, column=0, columnspan=COLS, rowspan=1, sticky="ew", pady=(0, 2))
            else:
                lane["link_in"].grid(row=top, column=1, columnspan=2, rowspan=n, sticky="nsew")
                lane["rule"].grid(row=top, column=3, columnspan=1, rowspan=n, sticky="ew", pady=0)
                lane["link_out"].grid(row=top, column=4, columnspan=1, rowspan=n, sticky="nsew")
                lane["toggle_box"].grid(row=top, column=5, columnspan=1, rowspan=n, sticky="ew", pady=0)
                tk.Misc.lower(lane["link_in"])  # the values stay above the canvas their lines start under (Canvas.lower is for items)
        self.sig = None
        c.after_idle(self.relayout)

    def relayout(self):
        """Redraw the lines (the geometry changed, or the text a line starts after got longer or shorter)."""
        if self.compact or not self.pc_on:
            return
        self.card.update_idletasks()
        for name, lane in self.lanes.items():
            if lane["link_in"].winfo_ismapped():
                lane["link_in"].lane = self.card.grid_bbox(2, lane["top"] + 1)[2]
                lane["link_in"].layout()
                lane["link_out"].layout()

    def refresh_lines(self):
        """Cheap check after every refresh: redraw the lines only when a source text changed its width."""
        sig = tuple(w.winfo_reqwidth() for lane in self.lanes.values() for w in lane["link_in"].sources)
        if sig != self.sig:
            self.sig = sig
            self.relayout()

    # ---- content
    def set_rule(self, name, verdict, vcolor, nums, ncolor, notes, tip, flow, fcolor):
        """The rule check of one lane: verdict word, the numbers behind it, up to two notes, tooltip; `flow`/`fcolor`
        = the line from the status into it (None = no reading, nothing flows)."""
        lane = self.lanes[name]
        for lb, (text, color) in zip(lane["rule_lines"], ((verdict, vcolor), (nums, ncolor), (notes[0], MUTED), (notes[1], MUTED))):
            lb.config(text=text, fg=color)
            lb.grid() if text else lb.grid_remove()
        lane["rule_tip"] = tip
        lane["link_in"].set_flow("idle" if flow is None else flow, fcolor)

    def set_agents(self, name, on, enabled, label, color, tip, flow, fcolor):
        """The agent switch of one lane: position (= what Paperclip has), label, tooltip and the line from the rule."""
        lane = self.lanes[name]
        if lane["var"].get() != on:
            lane["var"].set(on)
        lane["switch"].configure(state="normal" if enabled else "disabled")
        lane["switch"].set_label(label, color)
        lane["tip"] = tip
        lane["link_out"].set_flow(flow, fcolor)

    def tick(self, t):
        """Animation step (seconds): move the balls of every visible line."""
        if self.compact or not self.pc_on or not self.card.winfo_ismapped():
            return
        for lane in self.lanes.values():
            if lane["link_in"].winfo_ismapped():
                lane["link_in"].tick(t)
                lane["link_out"].tick(t)
