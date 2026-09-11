#!/usr/bin/env python3
"""
starstack button -- the one button, as an actual button.

Pick a folder. Press STACK. Watch the owl grumble. Get a picture.

Runs starstack.py as a subprocess so the CLI stays the source of truth;
this is just a face for it. Standard library only (tkinter), no extra
installs beyond what starstack itself needs.
"""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog

FROZEN = getattr(sys, "frozen", False)                 # running as starstack.exe
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
STARSTACK = os.path.join(HERE, "starstack.py")
IMAGE_EXT = {".tif", ".tiff", ".fit", ".fits", ".fts", ".png", ".jpg", ".jpeg"}

NAVY, NAVY2, INK, CREAM, MUTE = "#0f172a", "#172033", "#0b1220", "#f4efe6", "#8a93ad"
RED, RED_DARK, RED_LIT = "#d7301c", "#8c1a0f", "#ff6b57"
MONO = ("Consolas", 10) if sys.platform.startswith("win") else ("DejaVu Sans Mono", 10)
SANS = ("Segoe UI", 11) if sys.platform.startswith("win") else ("DejaVu Sans", 11)


def has_images(folder):
    try:
        return any(os.path.splitext(f)[1].lower() in IMAGE_EXT for f in os.listdir(folder))
    except OSError:
        return False


def is_sorted(folder):
    """Sorted layout: a lights/ subfolder with frames in it (Seestar), or
    frames/ (a scopepull observation)."""
    for d in ("lights", "light", "frames"):
        p = os.path.join(folder, d)
        if os.path.isdir(p) and has_images(p):
            return True
    return False


def is_session(folder):
    return os.path.isdir(folder) and (has_images(folder) or is_sorted(folder))


def night_sessions(folder, deeper=True):
    """Session folders under a night, Seestar-aware: `M81/` (results) and
    `M81_sub/` (frames) count once, as the `_sub`. A scopepull archive root
    (<root>/<date>/<observation>/) is a folder of nights; one level down."""
    out = []
    for d in sorted(os.listdir(folder)):
        p = os.path.join(folder, d)
        if not os.path.isdir(p) or d.lower().endswith(".partial"):
            continue
        if d.lower() == "stacks" and os.path.exists(os.path.join(p, "night.log")):
            continue                                        # our own earlier output
        if not is_session(p):
            if deeper and not has_images(p):
                out += night_sessions(p, deeper=False)
            continue
        if not d.lower().endswith("_sub") and os.path.isdir(p + "_sub") and has_images(p + "_sub"):
            continue
        out.append(p)
    return out


def is_night(folder):
    """A folder of session folders rather than a folder of frames."""
    if not os.path.isdir(folder) or is_session(folder):
        return False
    return bool(night_sessions(folder))


def target_name(folder):
    """Name the output after the target when the scope tells us."""
    name = None
    for fname, key in (("manifest.json", None), ("observation.json", "scope_manifest")):
        mpath = os.path.join(folder, fname)
        if os.path.exists(mpath):
            try:
                import json
                with open(mpath) as fh:
                    mf = json.load(fh)
                name = ((mf.get(key) or {}) if key else mf).get("nameTarget")
                break
            except Exception:
                pass
    name = (name or os.path.basename(folder.rstrip("/\\"))).strip()
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "-")
    return name.strip(" .") or "stack"


class Button(tk.Canvas):
    """The Big Red Button. Round, glossy, presses down."""

    def __init__(self, master, size=150, command=None, **kw):
        super().__init__(master, width=size, height=size, bg=NAVY, highlightthickness=0, **kw)
        self.size, self.command, self.enabled = size, command, True
        self.label = "STACK"
        self.draw(pressed=False)
        self.bind("<ButtonPress-1>", lambda e: self.enabled and self.draw(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)

    def _release(self, _):
        if not self.enabled:
            return
        self.draw(pressed=False)
        if self.command:
            self.command()

    def draw(self, pressed):
        self.delete("all")
        s = self.size; c = s / 2; r = s * 0.40
        lift = 0 if pressed else s * 0.05
        # bezel + well
        self.create_oval(c - r * 1.22, c - r * 1.22, c + r * 1.22, c + r * 1.22, fill="#5b6068", outline="#2a2e35", width=3)
        self.create_oval(c - r * 1.08, c - r * 1.08, c + r * 1.08, c + r * 1.08, fill="#1c1f24", outline="")
        # side of the cap, then the cap itself sitting higher
        col = RED if self.enabled else "#6b3a35"
        self.create_oval(c - r, c - r + lift * 0.4, c + r, c + r + lift * 0.4, fill=RED_DARK, outline="")
        self.create_oval(c - r, c - r - lift, c + r, c + r - lift, fill=col, outline="")
        self.create_oval(c - r * 0.55, c - r * 0.85 - lift, c + r * 0.15, c - r * 0.45 - lift,
                         fill=RED_LIT if self.enabled else "#8a5a55", outline="", stipple="gray50")
        size_pt = int(s * (0.17 if len(self.label) <= 5 else 0.13))
        self.create_text(c, c - lift + 2, text=self.label, fill="#fff4ee",
                         font=("Impact", size_pt) if sys.platform.startswith("win") else ("DejaVu Sans", int(size_pt * 0.82), "bold"))

    def set_enabled(self, on):
        self.enabled = on
        self.draw(pressed=False)

    def set_label(self, text):
        self.label = text
        self.draw(pressed=False)


# ---------------------------------------------------------------------------
# pausing: freeze the stacker and its worker processes where they stand
# ---------------------------------------------------------------------------
def _tree(proc):
    """The subprocess and its workers, via psutil when available."""
    try:
        import psutil
        p = psutil.Process(proc.pid)
        return [p] + p.children(recursive=True)
    except Exception:
        return None


def pause_tree(proc):
    procs = _tree(proc)
    if procs is not None:
        for p in procs:
            try:
                p.suspend()
            except Exception:
                pass
        return True
    if not sys.platform.startswith("win"):
        import signal
        os.kill(proc.pid, signal.SIGSTOP)      # no psutil: main process only
        return True
    return False


def resume_tree(proc):
    procs = _tree(proc)
    if procs is not None:
        for p in reversed(procs):
            try:
                p.resume()
            except Exception:
                pass
        return
    if not sys.platform.startswith("win"):
        import signal
        os.kill(proc.pid, signal.SIGCONT)


def kill_tree(proc):
    procs = _tree(proc)
    if procs is not None:
        for p in procs:
            try:
                p.resume()
            except Exception:
                pass
        for p in reversed(procs):              # workers first, then the parent
            try:
                p.terminate()
            except Exception:
                pass
        return
    try:
        resume_tree(proc)
    except Exception:
        pass
    proc.terminate()


# ---------------------------------------------------------------------------
# options: the overrides. everything here has a default the owl already chose.
# ---------------------------------------------------------------------------
DEFAULTS = {
    "bits16": True,        # 16-bit output (else 32-bit float)
    "quality": True,       # drop blurry / cloudy / starved frames
    "weights": True,       # weight survivors by sharpness and noise
    "align": True,         # star-align (off only if frames are already registered)
    "normalize": True,     # level-match frames before combining
    "darks": True,         # use darks found by name / in darks/
    "report": True,        # write frames.csv next to the output
    "method": "sigma",     # sigma | mean | median
    "sigma": 3.0,          # clip threshold, in MADs
    "debayer": "auto",     # auto | RGGB | BGGR | GRBG | GBRG | none
    "jobs": 0,             # 0 = let it pick
    "out_dir": "",         # "" = next to the frames (stacked/ or stacks/)
    "scratch": "",         # "" = system temp; the 10+ GB cube goes here
}
LABELS = {  # what the main window says when something is off-default
    "bits16": lambda v: None if v else "32-bit",
    "quality": lambda v: None if v else "keep every frame",
    "weights": lambda v: None if v else "no weighting",
    "align": lambda v: None if v else "no alignment",
    "normalize": lambda v: None if v else "no level-match",
    "darks": lambda v: None if v else "no darks",
    "report": lambda v: None if v else "no report",
    "method": lambda v: None if v == "sigma" else v,
    "sigma": lambda v: None if abs(v - 3.0) < 1e-9 else f"sigma {v:g}",
    "debayer": lambda v: None if v == "auto" else f"debayer {v}",
    "jobs": lambda v: None if not v else f"{v} workers",
    "out_dir": lambda v: None,          # shown in its own row, not the summary
    "scratch": lambda v: None if not v else f"scratch: {v}",
}
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".starstack.json")


def load_settings():
    s = dict(DEFAULTS)
    try:
        import json
        with open(SETTINGS_FILE) as fh:
            saved = json.load(fh)
        s.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        import json
        with open(SETTINGS_FILE, "w") as fh:
            json.dump(s, fh, indent=2)
    except Exception:
        pass


def settings_to_args(s):
    """The overrides, as starstack flags. Defaults produce nothing."""
    a = []
    if s["bits16"]:
        a += ["--bits", "16"]
    if not s["quality"]:
        a += ["--keep-all"]
    if not s["weights"]:
        a += ["--no-weights"]
    if not s["align"]:
        a += ["--no-align"]
    if not s["normalize"]:
        a += ["--no-normalize"]
    if not s["darks"]:
        a += ["--no-darks"]
    if s["method"] != "sigma":
        a += ["--method", s["method"]]
    if abs(float(s["sigma"]) - 3.0) > 1e-9:
        a += ["--sigma", f"{float(s['sigma']):g}"]
    if s["debayer"] != "auto":
        a += ["--debayer", s["debayer"]]
    if s["jobs"]:
        a += ["-j", str(int(s["jobs"]))]
    if s.get("scratch"):
        a += ["--scratch", s["scratch"]]
    return a


def summarize(s):
    parts = [LABELS[k](s[k]) for k in DEFAULTS]
    parts = [p for p in parts if p]
    return ("options: " + ", ".join(parts)) if parts else ""


class OptionsDialog(tk.Toplevel):
    """The overrides, with plain names and the default marked on each."""

    def __init__(self, master, settings, on_close):
        super().__init__(master)
        self.title("starstack options")
        self.configure(bg=NAVY)
        self.resizable(False, False)
        self.transient(master)
        self.settings, self.on_close = settings, on_close
        self.vars = {}
        pad = {"padx": 18, "pady": 3}

        def section(text):
            tk.Label(self, text=text, fg=MUTE, bg=NAVY, font=(SANS[0], 10, "bold")).pack(anchor="w", padx=18, pady=(12, 2))

        def check(key, text):
            v = tk.BooleanVar(value=bool(settings[key])); self.vars[key] = v
            tk.Checkbutton(self, text=text + ("   (default)" if DEFAULTS[key] else ""), variable=v, bg=NAVY, fg="#c9cfe0",
                           selectcolor=NAVY2, activebackground=NAVY, activeforeground=CREAM, font=SANS,
                           anchor="w", highlightthickness=0, bd=0).pack(fill="x", **pad)

        def choice(key, text, options, default_note):
            row = tk.Frame(self, bg=NAVY); row.pack(fill="x", **pad)
            tk.Label(row, text=text, fg="#c9cfe0", bg=NAVY, font=SANS, width=30, anchor="w").pack(side="left")
            v = tk.StringVar(value=str(settings[key])); self.vars[key] = v
            m = tk.OptionMenu(row, v, *options)
            m.config(bg=NAVY2, fg=CREAM, activebackground="#22304a", activeforeground=CREAM, relief="flat",
                     highlightthickness=0, font=SANS, width=8)
            m["menu"].config(bg=NAVY2, fg=CREAM, activebackground="#22304a", activeforeground=CREAM, font=SANS)
            m.pack(side="left")
            tk.Label(row, text=default_note, fg=MUTE, bg=NAVY, font=(SANS[0], 9)).pack(side="left", padx=(8, 0))

        def number(key, text, default_note):
            row = tk.Frame(self, bg=NAVY); row.pack(fill="x", **pad)
            tk.Label(row, text=text, fg="#c9cfe0", bg=NAVY, font=SANS, width=30, anchor="w").pack(side="left")
            v = tk.StringVar(value=str(settings[key])); self.vars[key] = v
            tk.Entry(row, textvariable=v, width=6, bg=NAVY2, fg=CREAM, insertbackground=CREAM, relief="flat",
                     font=SANS, justify="center").pack(side="left", ipady=3)
            tk.Label(row, text=default_note, fg=MUTE, bg=NAVY, font=(SANS[0], 9)).pack(side="left", padx=(8, 0))

        tk.Label(self, text="Everything here already has an answer. Change it only if the owl got it wrong.",
                 fg=MUTE, bg=NAVY, font=SANS, wraplength=500, justify="left").pack(anchor="w", padx=18, pady=(14, 0))

        section("Frames")
        check("darks", "Use dark frames (found by name, or in a darks/ folder)")
        check("quality", "Drop blurry, cloudy and starved frames")
        check("weights", "Weight the rest by sharpness and noise")
        check("align", "Star-align every frame (off only if they're already registered)")
        check("normalize", "Level-match frames before combining")
        choice("debayer", "Bayer pattern", ["auto", "RGGB", "BGGR", "GRBG", "GBRG", "none"], "default: auto (the owl looks)")

        section("Combine")
        choice("method", "How frames are combined", ["sigma", "mean", "median"], "default: sigma")
        number("sigma", "Clip threshold (in MADs, for sigma)", "default: 3.0")
        number("jobs", "Worker processes (0 = let it pick)", "default: 0")

        def folder(key, text, default_note):
            row = tk.Frame(self, bg=NAVY); row.pack(fill="x", **pad)
            tk.Label(row, text=text, fg="#c9cfe0", bg=NAVY, font=SANS, width=30, anchor="w").pack(side="left")
            v = tk.StringVar(value=str(settings.get(key, ""))); self.vars[key] = v
            tk.Entry(row, textvariable=v, width=22, bg=NAVY2, fg=CREAM, insertbackground=CREAM, relief="flat",
                     font=(SANS[0], 9)).pack(side="left", ipady=3)

            def pick():
                d = filedialog.askdirectory(title=text, parent=self)
                if d:
                    v.set(d)
            tk.Button(row, text="…", command=pick, bg=NAVY2, fg=CREAM, activebackground="#22304a",
                      activeforeground=CREAM, relief="flat", padx=6, font=SANS).pack(side="left", padx=(4, 0))
            tk.Label(row, text=default_note, fg=MUTE, bg=NAVY, font=(SANS[0], 9)).pack(side="left", padx=(8, 0))

        folder("scratch", "Scratch folder for the temporary cube", "default: system temp (needs 10+ GB)")

        section("Output")
        check("bits16", "16-bit output (off = 32-bit float)")
        check("report", "Write frames.csv with per-frame numbers next to the output")

        foot = tk.Frame(self, bg=NAVY); foot.pack(fill="x", padx=18, pady=(16, 14))
        tk.Button(foot, text="Reset to how the owl likes it", command=self.reset, bg=NAVY2, fg=CREAM,
                  activebackground="#22304a", activeforeground=CREAM, relief="flat", padx=10, font=SANS).pack(side="left")
        tk.Button(foot, text="Done", command=self.close, bg=RED, fg="#fff4ee", activebackground=RED_DARK,
                  activeforeground="#fff4ee", relief="flat", padx=18, font=(SANS[0], 11, "bold")).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())

    def reset(self):
        for k, v in DEFAULTS.items():
            self.vars[k].set(v if not isinstance(self.vars[k], tk.StringVar) else str(v))

    def collect(self):
        s = dict(self.settings)
        for k, var in self.vars.items():
            val = var.get()
            if isinstance(DEFAULTS[k], bool):
                s[k] = bool(val)
            elif isinstance(DEFAULTS[k], float):
                try:
                    s[k] = float(val)
                except ValueError:
                    s[k] = DEFAULTS[k]
            elif isinstance(DEFAULTS[k], int):
                try:
                    s[k] = max(0, int(float(val)))
                except ValueError:
                    s[k] = DEFAULTS[k]
            else:
                s[k] = str(val)
        return s

    def close(self):
        self.on_close(self.collect())
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("starstack")
        self.configure(bg=NAVY)
        self._set_icon()
        self.geometry("880x720")
        self.minsize(720, 560)
        self.proc = None
        self.paused = False
        self.q = queue.Queue()
        self.preview_path = None
        self.out_dir = None
        self.settings = load_settings()
        self._build()
        self.after(80, self._pump)

    def _set_icon(self):
        """The owl in the title bar and taskbar."""
        ico = os.path.join(HERE, "docs", "starstack.ico")
        png = os.path.join(HERE, "docs", "icon_256.png")
        try:
            if sys.platform.startswith("win") and os.path.exists(ico):
                self.iconbitmap(default=ico)
            elif os.path.exists(png):
                self._icon_img = tk.PhotoImage(file=png)
                self.iconphoto(True, self._icon_img)
        except tk.TclError:
            pass

    # ---- layout -------------------------------------------------------------
    def _build(self):
        top = tk.Frame(self, bg=NAVY); top.pack(fill="x", padx=18, pady=(14, 6))
        # the owl has degrees of grumpy. he does not have a smile.
        self.faces = {}
        for name in ("grumpy", "focused", "waiting", "judging", "grudging", "furious", "blink"):
            p = os.path.join(HERE, "docs", "faces", f"owl_{name}.png")
            if os.path.exists(p):
                try:
                    self.faces[name] = tk.PhotoImage(file=p)
                except tk.TclError:
                    pass
        if not self.faces:
            owl_path = os.path.join(HERE, "docs", "owl_256.png")
            if os.path.exists(owl_path):
                try:
                    self.faces["grumpy"] = tk.PhotoImage(file=owl_path).subsample(2, 2)
                except tk.TclError:
                    pass
        self.face_lbl = tk.Label(top, bg=NAVY)
        self.face_lbl.pack(side="left", padx=(0, 14))
        self.face = None
        self._blink_job = None
        self.set_face("grumpy")
        words = tk.Frame(top, bg=NAVY); words.pack(side="left", fill="x", expand=True)
        tk.Label(words, text="starstack", fg=CREAM, bg=NAVY, font=(SANS[0], 30, "bold")).pack(anchor="w")
        tk.Label(words, text="Stacking for people who'd rather be looking up.", fg="#c9cfe0", bg=NAVY, font=SANS).pack(anchor="w")
        self.mode_lbl = tk.Label(words, text="", fg=MUTE, bg=NAVY, font=MONO); self.mode_lbl.pack(anchor="w", pady=(6, 0))

        self.button = Button(top, size=150, command=self.stack); self.button.pack(side="right")

        row = tk.Frame(self, bg=NAVY); row.pack(fill="x", padx=18, pady=(4, 8))
        tk.Label(row, text="Folder", fg=MUTE, bg=NAVY, font=SANS).pack(side="left", padx=(0, 8))
        self.folder = tk.StringVar()
        self.folder.trace_add("write", lambda *_: self._describe())
        e = tk.Entry(row, textvariable=self.folder, bg=NAVY2, fg=CREAM, insertbackground=CREAM,
                     relief="flat", font=MONO)
        e.pack(side="left", fill="x", expand=True, ipady=6)
        tk.Button(row, text="Browse…", command=self.browse, bg=NAVY2, fg=CREAM, activebackground="#22304a",
                  activeforeground=CREAM, relief="flat", padx=12, font=SANS).pack(side="left", padx=(8, 0))

        orow = tk.Frame(self, bg=NAVY); orow.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(orow, text="Output", fg=MUTE, bg=NAVY, font=SANS).pack(side="left", padx=(0, 8))
        self.out_var = tk.StringVar(value=self.settings.get("out_dir", ""))
        self.out_entry = tk.Entry(orow, textvariable=self.out_var, bg=NAVY2, fg=CREAM, insertbackground=CREAM,
                                  relief="flat", font=MONO)
        self.out_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.out_hint = tk.Label(orow, text="", fg=MUTE, bg=NAVY, font=(SANS[0], 9))
        tk.Button(orow, text="Browse…", command=self.browse_out, bg=NAVY2, fg=CREAM, activebackground="#22304a",
                  activeforeground=CREAM, relief="flat", padx=12, font=SANS).pack(side="left", padx=(8, 0))
        tk.Button(orow, text="Clear", command=lambda: self.out_var.set(""), bg=NAVY2, fg="#c9cfe0",
                  activebackground="#22304a", activeforeground=CREAM, relief="flat", padx=8, font=SANS).pack(side="left", padx=(6, 0))
        self.out_var.trace_add("write", lambda *_: self._out_changed())
        self.out_entry.bind("<FocusIn>", lambda e: self._out_placeholder(False))
        self.out_entry.bind("<FocusOut>", lambda e: self._out_placeholder(True))
        self._out_placeholder(True)

        opts = tk.Frame(self, bg=NAVY); opts.pack(fill="x", padx=18)
        tk.Button(opts, text="Options…", command=self.open_options, bg=NAVY2, fg="#c9cfe0",
                  activebackground="#22304a", activeforeground=CREAM, relief="flat", padx=10, font=SANS).pack(side="left")
        self.opts_lbl = tk.Label(opts, text=summarize(self.settings), fg=MUTE, bg=NAVY, font=MONO)
        self.opts_lbl.pack(side="left", padx=(12, 0))

        body = tk.PanedWindow(self, orient="vertical", bg=NAVY, sashwidth=6, sashrelief="flat")
        body.pack(fill="both", expand=True, padx=18, pady=(6, 10))
        logbox = tk.Frame(body, bg=INK)
        self.log = tk.Text(logbox, bg=INK, fg="#d5dae6", insertbackground=CREAM, relief="flat",
                           font=MONO, wrap="word", state="disabled", padx=10, pady=8,
                           cursor="arrow", takefocus=1)
        sb = tk.Scrollbar(logbox, orient="vertical", command=self.log.yview,
                          bg=NAVY2, troughcolor=INK, activebackground="#22304a", relief="flat", width=14)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("owl", foreground="#f0cf96")
        self.log.tag_configure("bad", foreground="#ff8a7a")
        self.log.tag_configure("good", foreground="#9be59b")
        # keyboard: click the log (or Tab to it) and use the arrows / PgUp / PgDn / Home / End.
        # a disabled Text ignores keys, so drive the view ourselves.
        keys = {"<Up>": ("scroll", -1, "units"), "<Down>": ("scroll", 1, "units"),
                "<Prior>": ("scroll", -1, "pages"), "<Next>": ("scroll", 1, "pages"),
                "<Home>": ("moveto", 0.0), "<End>": ("moveto", 1.0)}

        def _nav(action):
            def handler(_event):
                if isinstance(self.focus_get(), tk.Entry):
                    return None                 # typing a path: leave the keys alone
                self.log.yview(*action)
                return "break"
            return handler
        for key, action in keys.items():
            self.bind_all(key, _nav(action))
        self.log.bind("<Button-1>", lambda e: self.log.focus_set())
        body.add(logbox, minsize=160)
        self.preview_frame = tk.Frame(body, bg=INK)
        self.preview_lbl = tk.Label(self.preview_frame, bg=INK, fg=MUTE, font=SANS,
                                    text="the picture shows up here")
        self.preview_lbl.pack(fill="both", expand=True)
        body.add(self.preview_frame, minsize=120)
        self.body = body
        # log on top, picture below, roughly half and half once the window has a size
        self.after(150, lambda: body.sash_place(0, 0, max(200, int(body.winfo_height() * 0.42))))
        self._resize_job = None
        self.preview_frame.bind("<Configure>", self._on_resize)

        foot = tk.Frame(self, bg=NAVY); foot.pack(fill="x", padx=18, pady=(0, 12))
        self.open_btn = tk.Button(foot, text="Open output folder", command=self.open_out, state="disabled",
                                  bg=NAVY2, fg=CREAM, activebackground="#22304a", activeforeground=CREAM,
                                  relief="flat", padx=12, font=SANS)
        self.open_btn.pack(side="left")
        self.stop_btn = tk.Button(foot, text="Stop", command=self.stop, state="disabled",
                                  bg=NAVY2, fg=CREAM, activebackground="#22304a", activeforeground=CREAM,
                                  relief="flat", padx=12, font=SANS)
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.status = tk.Label(foot, text="pick a folder.", fg=MUTE, bg=NAVY, font=SANS); self.status.pack(side="right")

        if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
            # a folder was dropped on the button: that IS the button press
            self.folder.set(sys.argv[1])
            self.after(600, self.stack)

    # ---- the face -----------------------------------------------------------
    def set_face(self, name):
        img = self.faces.get(name) or self.faces.get("grumpy")
        if img is not None:
            self.face_lbl.config(image=img)
        if name != "blink":
            self.face = name
            self._face_seq = getattr(self, "_face_seq", 0) + 1

    def _blink(self):
        """Every few seconds while working, so he reads as alive, not a sticker."""
        self._blink_job = None
        running = self.proc is not None and self.proc.poll() is None
        if not running or self.paused or self.face == "waiting" or "blink" not in self.faces:
            return
        current = self.face
        self.set_face("blink")
        self.after(110, lambda: self.set_face(current))
        self._schedule_blink()

    def _schedule_blink(self):
        import random
        if self._blink_job:
            self.after_cancel(self._blink_job)
        self._blink_job = self.after(random.randint(2500, 6000), self._blink)

    # ---- output folder ------------------------------------------------------
    PLACEHOLDER = "next to the frames  (stacked\\ for a session, stacks\\ for a night)"

    def _out_placeholder(self, show):
        """Grey hint inside the empty box; real text otherwise."""
        v = self.out_var.get()
        if show and not v.strip():
            self._showing_hint = True
            self.out_entry.config(fg=MUTE)
            self.out_var.set(self.PLACEHOLDER)
        elif not show and getattr(self, "_showing_hint", False):
            self._showing_hint = False
            self.out_var.set("")
            self.out_entry.config(fg=CREAM)

    def _out_changed(self):
        v = self.out_var.get()
        if v == self.PLACEHOLDER:
            return
        self.out_entry.config(fg=CREAM)
        self._showing_hint = False
        self.settings["out_dir"] = v.strip().strip('"')
        save_settings(self.settings)
        if not v.strip() and self.focus_get() is not self.out_entry:
            self.after(10, lambda: self._out_placeholder(True))

    def chosen_out_dir(self):
        v = self.settings.get("out_dir", "").strip()
        return v if v and v != self.PLACEHOLDER else ""

    def browse_out(self):
        d = filedialog.askdirectory(title="Where the stacks go (leave empty for next to the frames)")
        if d:
            self._showing_hint = False
            self.out_entry.config(fg=CREAM)
            self.out_var.set(d)

    # ---- options ------------------------------------------------------------
    def open_options(self):
        def done(s):
            self.settings = s
            save_settings(s)
            self.opts_lbl.config(text=summarize(s))
        OptionsDialog(self, self.settings, done)

    # ---- behaviour ----------------------------------------------------------
    def browse(self):
        d = filedialog.askdirectory(title="Folder of frames, or a folder of session folders")
        if d:
            self.folder.set(d)

    def _describe(self):
        f = self.folder.get().strip().strip('"')
        if not os.path.isdir(f):
            self.mode_lbl.config(text=""); return
        if is_night(f):
            n = len(night_sessions(f))
            self.mode_lbl.config(text=f"whole night: {n} session folders. One press does all of them.")
            return
        elif is_sorted(f):
            ld = os.path.join(f, "lights" if os.path.isdir(os.path.join(f, "lights")) else "light")
            n = sum(1 for x in os.listdir(ld) if os.path.splitext(x)[1].lower() in IMAGE_EXT)
            self.mode_lbl.config(text=f"sorted layout, {n} lights. Output: {target_name(f)}.tif")
        elif has_images(f):
            n = sum(1 for x in os.listdir(f) if os.path.splitext(x)[1].lower() in IMAGE_EXT)
            self.mode_lbl.config(text=f"{n} image files. Output: {target_name(f)}.tif")
        else:
            self.mode_lbl.config(text="no images in there.")

    def say(self, text, tag=None):
        at_bottom = self.log.yview()[1] >= 0.999
        self.log.configure(state="normal")
        if text.startswith("\r"):
            # progress line: replace the last line instead of appending
            self.log.delete("end-2l", "end-1c")
            self.log.insert("end", "\n" + text[1:].rstrip("\n"), tag)
        else:
            self.log.insert("end", text, tag)
        if at_bottom:                      # follow the log unless the reader scrolled up
            self.log.see("end")
        self.log.configure(state="disabled")

    def stack(self):
        if self.proc and self.proc.poll() is None:          # running: the button pauses / resumes
            self.toggle_pause(); return
        f = self.folder.get().strip().strip('"')
        if not os.path.isdir(f):
            self.say("pick a folder first. I can't stack a feeling.\n", "bad"); return
        chosen = self.chosen_out_dir()
        if chosen and not os.path.isdir(chosen):
            try:
                os.makedirs(chosen, exist_ok=True)
            except OSError as e:
                self.say(f"can't create the output folder {chosen}: {e}\n", "bad"); return
        if is_night(f):
            self.out_dir = chosen or os.path.join(f, "stacks")
            out_arg = self.out_dir
            self.preview_path = None
        elif is_session(f):
            self.out_dir = chosen or os.path.join(f, "stacked")
            os.makedirs(self.out_dir, exist_ok=True)
            name = target_name(f)
            out_arg = os.path.join(self.out_dir, name + ".tif")
            self.preview_path = os.path.join(self.out_dir, name + "_look.png")
        else:
            self.say("no image files in that folder.\n", "bad"); return

        if FROZEN:
            cmd = [sys.executable, "--cli", f, "-o", out_arg]   # the exe, in CLI mode
        else:
            cmd = [os.environ.get("STARSTACK_PYTHON", sys.executable), "-u", STARSTACK, f, "-o", out_arg]
        if self.preview_path:
            cmd += ["--preview", self.preview_path]
        cmd += settings_to_args(self.settings)
        if self.settings["report"]:
            cmd += ["--report", os.path.join(self.out_dir, "frames.csv")] if self.preview_path else ["--report", "x"]

        self.log.configure(state="normal"); self.log.delete("1.0", "end"); self.log.configure(state="disabled")
        self.preview_lbl.config(image="", text="working…")
        self.paused = False
        self.button.set_label("PAUSE"); self.stop_btn.config(state="normal"); self.open_btn.config(state="disabled")
        self.status.config(text="stacking…")
        self.set_face("focused")
        self._schedule_blink()
        threading.Thread(target=self._run, args=(cmd,), daemon=True).start()

    def _run(self, cmd):
        try:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1, creationflags=flags, errors="replace", env=env)
        except Exception as e:
            self.q.put(("line", f"couldn't start starstack: {e}\n")); self.q.put(("done", 1)); return
        buf = ""
        while True:
            ch = self.proc.stdout.read(1)
            if not ch:
                break
            buf += ch
            if ch in "\n\r":
                if ch == "\r":
                    # progress; emit as a replace-last-line message
                    self.q.put(("line", "\r" + buf.rstrip("\r")))
                else:
                    self.q.put(("line", buf))
                buf = ""
        if buf:
            self.q.put(("line", buf))
        self.q.put(("done", self.proc.wait()))

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    text = payload
                    tag = None
                    low = text.lower()
                    if "you're welcome" in low or "they know what they did" in low or "relax" in low or "go outside" in low or "go to bed" in low:
                        tag = "owl"
                    elif "wouldn't" in low or "failed" in low or "couldn't" in low or "error" in low:
                        tag = "bad"
                    elif text.startswith("  used") or text.startswith("  output"):
                        tag = "good"
                    self.say(text, tag)
                    self._react(low)
                else:
                    self._finished(payload)
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _glance(self, name, back, ms):
        """Pull a face for a moment, then return to `back` -- unless something
        else changed his mood in the meantime."""
        self.set_face(name)
        seq = self._face_seq

        def revert():
            if self._face_seq == seq and self.running() and not self.paused:
                self.set_face(back)
        self.after(ms, revert)

    def _react(self, low):
        """Which grumpy, based on what the owl just said."""
        if low.startswith("  registering") or "reference:" in low or "debayering" in low:
            if self.face != "focused":
                self.set_face("focused")
        elif "combining" in low:
            if self.face != "waiting":
                self.set_face("waiting")        # eyes shut. wake him when it's over.
        elif "dropped" in low and "quality check" in low:
            self._glance("judging", "focused", 3000)
        elif "removed it from the pile" in low or "not mixing pipelines" in low:
            self._glance("judging", "focused", 2000)
        elif "writing " in low:
            self.set_face("focused")

    def _finished(self, rc):
        self.paused = False
        self.button.set_label("STACK"); self.button.set_enabled(True); self.stop_btn.config(state="disabled")
        self.open_btn.config(state="normal")
        if self._blink_job:
            self.after_cancel(self._blink_job); self._blink_job = None
        if rc == 0:
            self.status.config(text="done. go outside.")
            self.set_face("grudging")           # the most he will give you
            self._show_preview()
        else:
            self.status.config(text=f"stopped (exit {rc}). read the log.")
            self.set_face("furious")
            self.preview_lbl.config(image="", text="no picture this time")

    def _on_resize(self, _):
        if self.preview_path is None and not self.out_dir:
            return
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(200, lambda: (not self.running()) and self._show_preview())

    def _show_preview(self):
        path = self.preview_path
        if not path and self.out_dir and os.path.isdir(self.out_dir):
            looks = sorted(p for p in os.listdir(self.out_dir) if p.endswith("_look.png"))
            path = os.path.join(self.out_dir, looks[-1]) if looks else None
        if not path or not os.path.exists(path):
            self.preview_lbl.config(image="", text="no preview written"); return
        try:
            img = tk.PhotoImage(file=path)
            w = max(200, self.preview_frame.winfo_width() - 20)
            h = max(120, self.preview_frame.winfo_height() - 20)
            k = max(1, int(-(-max(img.width() / w, img.height() / h) // 1)))   # ceil
            if k > 1:
                img = img.subsample(k, k)
            self.preview_img = img
            self.preview_lbl.config(image=img, text="")
        except tk.TclError as e:
            self.preview_lbl.config(image="", text=f"couldn't show preview: {e}")

    def open_out(self):
        d = self.out_dir
        if not d or not os.path.isdir(d):
            return
        if sys.platform.startswith("win"):
            os.startfile(d)                       # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", d])
        else:
            subprocess.Popen(["xdg-open", d])

    def toggle_pause(self):
        if not (self.proc and self.proc.poll() is None):
            return
        if not self.paused:
            if not pause_tree(self.proc):
                self.say("\ncan't pause on this system (pip install psutil). Stop is over there.\n", "bad"); return
            self.paused = True
            self.button.set_label("RESUME")
            self.status.config(text="paused.")
            if self._blink_job:
                self.after_cancel(self._blink_job); self._blink_job = None
            self._face_before_pause = self.face
            self.set_face("waiting")
            self.say("\npaused. fine. the photons will wait. (the time-left estimate won't know about this.)\n", "owl")
        else:
            resume_tree(self.proc)
            self.paused = False
            self.button.set_label("PAUSE")
            self.status.config(text="stacking…")
            self.set_face(getattr(self, "_face_before_pause", "focused"))
            self._schedule_blink()
            self.say("resuming.\n", "owl")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            kill_tree(self.proc)
            self.paused = False
            self.say("\nstopped. fine.\n", "bad")


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
