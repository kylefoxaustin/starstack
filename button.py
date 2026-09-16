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
        size_pt = int(s * (0.17 if len(self.label) <= 5 else 0.13 if len(self.label) <= 8 else 0.105))
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
def make_job():
    """Windows only: a job object marked KILL_ON_JOB_CLOSE. Every process we
    put in it -- the stacker, its workers, the scopepull under it -- is ended
    by Windows the moment the last handle to the job closes, and our handle
    closes when the owl dies, however it dies: X button, Task Manager, crash.
    That is the guarantee the X-button handler alone can't give (Task Manager
    runs none of our code). Returns the handle, or None off Windows."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                                                        "OtherOperationCount", "ReadTransferCount",
                                                        "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):   # 9 = ExtendedLimitInformation
            k32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def join_job(job, proc):
    """Put a just-started subprocess (and, by inheritance, everything it will
    start) into the job. Quietly does nothing off Windows or if the job
    couldn't be made."""
    if job is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        return bool(k32.AssignProcessToJobObject(job, int(proc._handle)))
    except Exception:
        return False


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
    "pull": False,         # fetch new observations off an Odyssey Pro (scopepull) before stacking
    "pull_target": "",     # with pull: only observations whose target matches this
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
    "pull": lambda v: None,             # has its own row too
    "pull_target": lambda v: None,
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


# ---------------------------------------------------------------------------
# scopepull: the fetch half. starstack shells out to it; the button just needs
# to know whether it's there and where it keeps the archive.
# ---------------------------------------------------------------------------
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0


def find_scopepull():
    """Path to the scopepull command, or None. PATH first; then the places
    pipx, uv and Python's own Scripts folder put console scripts -- because
    on Windows a double-clicked exe inherits Explorer's PATH from sign-in
    time, which doesn't know about a `pipx ensurepath` run this afternoon."""
    import glob
    import shutil
    exe = shutil.which("scopepull")
    if exe:
        return exe
    home = os.path.expanduser("~")
    names = ("scopepull.exe", "scopepull") if sys.platform.startswith("win") else ("scopepull",)
    candidates = [os.path.join(home, ".local", "bin"),                                   # pipx / uv default
                  os.path.join(os.environ.get("PIPX_BIN_DIR", ""), ""),
                  os.path.join(os.environ.get("LOCALAPPDATA", ""), "pipx", "venvs", "scopepull", "Scripts"),
                  os.path.join(home, ".local", "pipx", "venvs", "scopepull", "bin"),
                  os.path.join(home, "pipx", "venvs", "scopepull", "Scripts"),
                  os.path.join(os.environ.get("APPDATA", ""), "Python", "Python31*", "Scripts"),
                  os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Python31*", "Scripts"),
                  os.path.join(home, ".local", "share", "uv", "tools", "scopepull", "Scripts"),
                  os.path.join(home, ".local", "share", "uv", "tools", "scopepull", "bin")]
    for d in candidates:
        if not d.strip(os.sep):
            continue
        for name in names:
            for hit in sorted(glob.glob(os.path.join(d, name)), reverse=True):
                if os.path.isfile(hit) and os.access(hit, os.X_OK):
                    return hit
    return None


def find_system_python(min_version=(3, 11)):
    """A real CPython >= 3.11 on this machine (not the one frozen inside the
    exe -- that has no pip). Returns the command as a list, or None."""
    import glob
    import shutil
    cands = []
    if sys.platform.startswith("win"):
        py = shutil.which("py")
        if py:
            cands += [[py, "-3.13"], [py, "-3.12"], [py, "-3.11"], [py, "-3"]]
        for pat in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Python31[1-9]*", "python.exe"),
                    os.path.join(os.environ.get("ProgramFiles", ""), "Python31[1-9]*", "python.exe")):
            cands += [[p] for p in sorted(glob.glob(pat), reverse=True)]
    for name in ("python3", "python"):
        p = shutil.which(name)
        if p and "WindowsApps" not in p:          # the Store stub opens a shop window, not Python
            cands.append([p])
    if not FROZEN:
        cands.append([sys.executable])
    for cmd in cands:
        try:
            out = subprocess.run(cmd + ["-c", "import sys;print(sys.version_info[0],sys.version_info[1])"],
                                 capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW).stdout.split()
            if len(out) == 2 and (int(out[0]), int(out[1])) >= min_version:
                return cmd
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return None


def _vtuple(v):
    """'0.2.10' -> (0, 2, 10); anything odd -> () so it never wins a comparison."""
    import re
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", str(v or ""))
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def _fetch_json(url, timeout=3.0):
    """One small GET, no data sent, quiet on any failure (the scope's Wi-Fi has
    no internet, and that's fine)."""
    import json
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "starstack-button", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def latest_scopepull():
    """Newest scopepull version on PyPI, or None if we can't ask."""
    d = _fetch_json("https://pypi.org/pypi/scopepull/json")
    try:
        return d["info"]["version"]
    except (TypeError, KeyError):
        return None


def latest_starstack():
    """Newest starstack release tag on GitHub, or None."""
    d = _fetch_json("https://api.github.com/repos/kylefoxaustin/starstack/releases/latest")
    try:
        return d["tag_name"].lstrip("v")
    except (TypeError, KeyError, AttributeError):
        return None


def starstack_version():
    """__version__ out of starstack.py without importing numpy and friends."""
    import re
    try:
        with open(STARSTACK, encoding="utf-8") as fh:
            m = re.search(r'^__version__\s*=\s*"([^"]+)"', fh.read(), re.M)
        return m.group(1) if m else "?"
    except OSError:
        return "?"


def probe_scopepull():
    """(version, archive_root) if scopepull can be found, else (None, None).
    Runs two quick commands; call it off the UI thread."""
    exe = find_scopepull()
    if not exe:
        return None, None, None
    version, root = "?", None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20,
                             creationflags=NO_WINDOW).stdout
        version = out.strip().split()[-1] if out.strip() else "?"
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        out = subprocess.run([exe, "status"], capture_output=True, text=True, timeout=30,
                             creationflags=NO_WINDOW).stdout
        for line in out.splitlines():
            if line.strip().lower().startswith("archive root:"):
                root = os.path.expanduser(line.split(":", 1)[1].strip())
                break
    except (OSError, subprocess.SubprocessError):
        pass
    return version, root or os.path.join(os.path.expanduser("~"), "Astro", "odyssey"), exe


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
        self.job = make_job()                    # dies with us; takes every child along
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
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
        self.mode_lbl = tk.Label(words, text="", fg=MUTE, bg=NAVY, font=MONO, justify="left", wraplength=520)
        self.mode_lbl.pack(anchor="w", pady=(6, 0))

        self.button = Button(top, size=150, command=self.stack); self.button.pack(side="right")

        row = tk.Frame(self, bg=NAVY); row.pack(fill="x", padx=18, pady=(4, 8))
        tk.Label(row, text="Folder", fg=MUTE, bg=NAVY, font=SANS).pack(side="left", padx=(0, 8))
        self.folder = tk.StringVar()
        self.folder.trace_add("write", lambda *_: self._describe())
        self.folder_entry = tk.Entry(row, textvariable=self.folder, bg=NAVY2, fg=CREAM, insertbackground=CREAM,
                                     relief="flat", font=MONO)
        self.folder_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self._hint_bind(self.folder_entry, self.folder)
        tk.Button(row, text="Browse…", command=self.browse, bg=NAVY2, fg=CREAM, activebackground="#22304a",
                  activeforeground=CREAM, relief="flat", padx=12, font=SANS).pack(side="left", padx=(8, 0))

        # the Odyssey row: fetch off a Unistellar Odyssey Pro with scopepull before
        # stacking. scopepull speaks that scope's API and no other.
        # greyed out until a background probe finds scopepull on PATH.
        srow = tk.Frame(self, bg=NAVY); srow.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(srow, text="Odyssey", fg=MUTE, bg=NAVY, font=SANS).pack(side="left", padx=(0, 8))
        self.pull_var = tk.BooleanVar(value=bool(self.settings.get("pull")))
        self.pull_chk = tk.Checkbutton(srow, text="Pull new observations off the Odyssey Pro first", variable=self.pull_var,
                                       bg=NAVY, fg="#c9cfe0", selectcolor=NAVY2, activebackground=NAVY,
                                       activeforeground=CREAM, font=SANS, highlightthickness=0, bd=0,
                                       state="disabled", command=self._pull_changed)
        self.pull_chk.pack(side="left")
        tk.Label(srow, text="target", fg=MUTE, bg=NAVY, font=SANS).pack(side="left", padx=(14, 6))
        self.pull_target = tk.StringVar(value=self.settings.get("pull_target", ""))
        self.pull_target.trace_add("write", lambda *_: self._pull_changed())
        self.target_entry = tk.Entry(srow, textvariable=self.pull_target, width=12, bg=NAVY2, fg=CREAM,
                                     insertbackground=CREAM, relief="flat", font=MONO)
        self.target_entry.pack(side="left", ipady=4)
        self._hint_bind(self.target_entry, self.pull_target)
        shint = tk.Frame(self, bg=NAVY); shint.pack(fill="x", padx=18, pady=(0, 6))
        tk.Label(shint, text="", bg=NAVY, font=SANS, width=7).pack(side="left", padx=(0, 8))   # under "Odyssey"
        # the button is packed on the right, FIRST, so a long hint can never push it
        # off the edge of the window (0.2.11: it was only visible full-screen)
        self.install_btn = tk.Button(shint, text="Install scopepull…", command=self.install_scopepull,
                                     bg=NAVY2, fg=CREAM, activebackground="#22304a", activeforeground=CREAM,
                                     relief="flat", padx=8, font=(SANS[0], 9))   # shown only when needed
        self.scope_lbl = tk.Label(shint, text="looking for scopepull…", fg=MUTE, bg=NAVY, font=(SANS[0], 9), anchor="w")
        self.scope_lbl.pack(side="left", fill="x", expand=True)
        self.scopepull = (None, None, None)          # (version, archive root, exe path)
        threading.Thread(target=self._probe_scopepull, daemon=True).start()
        threading.Thread(target=self._check_self, daemon=True).start()

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

        self._refresh_hints()
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

    # ---- grey hints in empty boxes ------------------------------------------
    # What an empty box means, written in the box. The StringVar carries the
    # hint text while it shows, so readers go through _real() to get "".
    def _hint_bind(self, entry, var):
        entry._hint_on = False
        entry.bind("<FocusIn>", lambda e: self._hint_clear(entry, var))
        entry.bind("<FocusOut>", lambda e: self._refresh_hints())

    def _hint_clear(self, entry, var):
        if getattr(entry, "_hint_on", False):
            self._hinting = True
            entry._hint_on = False
            var.set("")
            entry.config(fg=CREAM)
            self._hinting = False

    def _hint_set(self, entry, var, text):
        """Show `text` greyed if the box is empty and not being typed in."""
        self._hinting = True
        try:
            if getattr(entry, "_hint_on", False):
                entry._hint_on = False
                var.set("")
            if text and not var.get().strip() and self.focus_get() is not entry:
                entry._hint_on = True
                entry._hint_text = text
                entry.config(fg=MUTE)
                var.set(text)
            else:
                entry.config(fg=CREAM)
        finally:
            self._hinting = False

    def _real(self, entry, var):
        """The box's real value: "" while it only shows its hint. A value set
        from code (a folder dropped on the exe, a test) while the hint was up
        counts as real and switches the hint off."""
        if getattr(entry, "_hint_on", False):
            if var.get() == getattr(entry, "_hint_text", None):
                return ""
            entry._hint_on = False
            entry.config(fg=CREAM)
        return var.get().strip().strip('"')

    def _refresh_hints(self):
        if not hasattr(self, "target_entry"):
            return
        if self.pulling():
            self._hint_set(self.folder_entry, self.folder,
                           f"empty = scopepull's archive, {self.scopepull[1]}   (or a folder to pull into and stack)")
            self._hint_set(self.target_entry, self.pull_target, "all targets")
        else:
            self._hint_set(self.folder_entry, self.folder, "a folder of frames, or a folder of session folders")
            self._hint_set(self.target_entry, self.pull_target, "")

    # ---- the scope ----------------------------------------------------------
    def _probe_scopepull(self):
        found = probe_scopepull()
        self.q.put(("scopepull", found))
        if found[0] is not None:                       # installed: is it current?
            self.q.put(("scopepull_latest", latest_scopepull()))

    def _check_self(self):
        mine, latest = starstack_version(), latest_starstack()
        if latest and _vtuple(latest) > _vtuple(mine):
            self.q.put(("starstack_latest", (mine, latest)))

    NO_SCOPEPULL = "needs scopepull, the Unistellar Odyssey Pro puller (Python 3.11+). The button installs it."

    def _scopepull_found(self, found):
        self.scopepull = found
        version, root, exe = found
        self.pull_chk.config(state="normal")
        if version is None:
            self.scope_lbl.config(text=self.NO_SCOPEPULL)
            self.install_btn.pack(side="right", padx=(10, 0))
            if self.pull_var.get():
                self.pull_var.set(False)
        else:
            self.install_btn.pack_forget()
            self.install_btn.config(text="Install scopepull…")
            d = os.path.dirname(exe)
            where = "" if d in os.environ.get("PATH", "").split(os.pathsep) else \
                f"  ·  found off PATH in …\\{os.path.basename(os.path.dirname(d))}\\{os.path.basename(d)}"
            self.scope_lbl.config(text=f"scopepull {version} (Unistellar Odyssey Pro)  ·  archive: {root}{where}")
            if where and getattr(self, "_explain_path", False):
                self._explain_path = False
                self.say(f"scopepull {version} lives in {d}. That folder isn't on your PATH; the owl doesn't "
                         f"need it to be. If you also want to type `scopepull` in PowerShell, add that folder "
                         f"to PATH (Settings > System > About > Advanced system settings > Environment Variables).\n", "owl")
        self._pull_changed(save=False)

    def _scopepull_latest(self, latest):
        """PyPI answered. If it's newer than what's installed, say so and turn
        the install button into an update button."""
        mine = self.scopepull[0]
        if not latest or mine is None or _vtuple(latest) <= _vtuple(mine):
            return
        self.scope_lbl.config(text=self.scope_lbl.cget("text").replace(
            f"scopepull {mine} ", f"scopepull {mine} -- {latest} is out -- ", 1))
        self.install_btn.config(text=f"Update scopepull to {latest}…")
        self.install_btn.pack(side="right", padx=(10, 0))

    def _starstack_latest(self, pair):
        mine, latest = pair
        self.say(f"psst: starstack {latest} is out (this is {mine}). "
                 f"github.com/kylefoxaustin/starstack/releases\n", "owl")

    def _pull_changed(self, save=True):
        if getattr(self, "_hinting", False):
            return
        if self.pull_var.get() and self.scopepull[0] is None:
            # the box was ticked but there is nothing to tick it for. say so, once, out loud.
            self.pull_var.set(False)
            if self.scopepull[2] is None and self.scope_lbl.cget("text") != "looking for scopepull…":
                self.say("can't pull: scopepull isn't installed on this machine. The 'Install scopepull…' "
                         "button next to that line does it. The Folder row still works for frames you already have.\n", "bad")
            return
        on = bool(self.pull_var.get()) and self.scopepull[0] is not None
        self.settings["pull"] = bool(self.pull_var.get())
        self.settings["pull_target"] = self._real(self.target_entry, self.pull_target)
        if save:
            save_settings(self.settings)
        if not self.running():
            self.button.set_label("PULL+STACK" if on else "STACK")
        self._refresh_hints()
        self._describe()

    def pulling(self):
        """Is the next press a pull-then-stack?"""
        return bool(self.pull_var.get()) and self.scopepull[0] is not None

    # ---- installing scopepull -----------------------------------------------
    def install_scopepull(self):
        """One click: pip-install scopepull with a Python on this machine, then
        look again. No Python? Ask winget for one. No winget? Open python.org."""
        if self.running():
            self.say("busy. Install it after the stack.\n", "bad"); return
        self.install_btn.config(state="disabled")
        self.log.configure(state="normal"); self.log.delete("1.0", "end"); self.log.configure(state="disabled")
        self._cr_pending = False
        verb = "updating" if self.scopepull[0] else "installing"
        self.say(f"{verb} scopepull. This needs a Python 3.11 or newer on this machine -- looking.\n", "owl")
        threading.Thread(target=self._install_scopepull, daemon=True).start()

    def _install_scopepull(self):
        py = find_system_python()
        if py is None and sys.platform.startswith("win"):
            import shutil
            winget = shutil.which("winget")
            if winget:
                self.q.put(("line", "no Python 3.11+ here. Asking winget to install Python 3.12 -- a minute or two.\n"))
                rc = self._stream([winget, "install", "--id", "Python.Python.3.12", "-e", "--silent",
                                   "--accept-package-agreements", "--accept-source-agreements"])
                if rc == 0:
                    py = find_system_python()
        if py is None:
            self.q.put(("line", "no Python 3.11+ on this machine and no way to fetch one. Get it from python.org "
                                "(tick 'Add python.exe to PATH'), then press Install again.\n"))
            try:
                import webbrowser
                webbrowser.open("https://www.python.org/downloads/windows/")
            except Exception:
                pass
            self.q.put(("install_done", 1)); return
        self.q.put(("line", f"using {' '.join(py)}\n"))
        rc = self._stream(py + ["-m", "pip", "install", "--user", "--upgrade", "--no-warn-script-location", "scopepull"])
        self.q.put(("install_done", rc))

    def _stream(self, cmd):
        """Run a helper command, its output into the log; return its exit code."""
        try:
            env = dict(os.environ, PYTHONUNBUFFERED="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
            if not sys.platform.startswith("win"):
                env["PIP_BREAK_SYSTEM_PACKAGES"] = "1"      # Debian/Ubuntu's "externally managed" refusal
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 bufsize=0, creationflags=NO_WINDOW, env=env)
            join_job(self.job, p)
        except OSError as e:
            self.q.put(("line", f"couldn't run {cmd[0]}: {e}\n")); return 1
        self._relay(p.stdout, prefix="  ")
        return p.wait()

    def _install_done(self, rc):
        self.install_btn.config(state="normal")
        if rc == 0:
            self.say("installed. Looking for it again.\n", "owl")
            self._explain_path = True
            self.scope_lbl.config(text="looking for scopepull…")
            threading.Thread(target=self._probe_scopepull, daemon=True).start()
        else:
            self.say(f"that didn't work (exit {rc}). The lines above say why. Fallback, in PowerShell: "
                     f"pipx install scopepull\n", "bad")

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
        if getattr(self, "_hinting", False):
            return
        f = self._real(self.folder_entry, self.folder)
        if getattr(self, "scopepull", (None, None, None))[0] is not None and self.pulling():
            where = f or self.scopepull[1]
            tgt = self._real(self.target_entry, self.pull_target)
            what = f"new {tgt!r} observations" if tgt else "everything new"
            self.mode_lbl.config(text=f"pull {what} off the Odyssey into {where}, then stack the archive "
                                      f"(targets already stacked are skipped)")
            return
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
        """Append to the log the way a terminal would: `\n` ends the line, `\r`
        means the next text overwrites the current line. That's how starstack's
        "combining rows 400/1094" and scopepull's "building 187/364 frames"
        update in place instead of scrolling the window off the bottom."""
        body = text.rstrip("\r\n")
        term = text[len(body):]
        at_bottom = self.log.yview()[1] >= 0.999
        self.log.configure(state="normal")
        if body:
            if getattr(self, "_cr_pending", False):
                self.log.delete("end-1c linestart", "end-1c")     # overwrite the current line
            self.log.insert("end-1c", body, tag)
            self._cr_pending = False
        if "\n" in term:
            self.log.insert("end-1c", "\n")
            self._cr_pending = False
        elif "\r" in term:
            self._cr_pending = True
        if at_bottom:                      # follow the log unless the reader scrolled up
            self.log.see("end")
        self.log.configure(state="disabled")

    def _relay(self, stream, prefix=""):
        """Read a child's stdout in bytes (text mode would turn every `\r` into
        `\n` and defeat say()), one segment per `\n` or `\r`, onto the queue."""
        import codecs
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        buf = ""
        while True:
            chunk = stream.read(1)
            if not chunk:
                break
            ch = dec.decode(chunk)
            if not ch:
                continue
            buf += ch
            if ch in "\n\r":
                self.q.put(("line", (prefix + buf) if buf.strip() else buf))
                buf = ""
        buf += dec.decode(b"", final=True)
        if buf:
            self.q.put(("line", prefix + buf + "\n"))

    def stack(self):
        if self.proc and self.proc.poll() is None:          # running: the button pauses / resumes
            self.toggle_pause(); return
        f = self._real(self.folder_entry, self.folder)
        pull = self.pulling()
        if not pull and not os.path.isdir(f):
            self.say("pick a folder first. I can't stack a feeling.\n", "bad"); return
        chosen = self.chosen_out_dir()
        if chosen and not os.path.isdir(chosen):
            try:
                os.makedirs(chosen, exist_ok=True)
            except OSError as e:
                self.say(f"can't create the output folder {chosen}: {e}\n", "bad"); return
        if pull:
            # scopepull fills the archive (the folder box, or its own configured
            # root); starstack then treats that archive as a night of nights.
            archive = f or self.scopepull[1]
            self.out_dir = chosen or os.path.join(archive, "stacks")
            out_arg = self.out_dir
            self.preview_path = None
        elif is_night(f):
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
            cmd = [sys.executable, "--cli"]                      # the exe, in CLI mode
        else:
            cmd = [os.environ.get("STARSTACK_PYTHON", sys.executable), "-u", STARSTACK]
        if pull:
            cmd += ["--pull"]
            tgt = self._real(self.target_entry, self.pull_target)
            if tgt:
                cmd += ["--pull-target", tgt]
            if f:
                cmd += [f]                                       # pull into (and stack) this folder
        else:
            cmd += [f]
        cmd += ["-o", out_arg]
        if self.preview_path:
            cmd += ["--preview", self.preview_path]
        cmd += settings_to_args(self.settings)
        if self.settings["report"]:
            cmd += ["--report", os.path.join(self.out_dir, "frames.csv")] if self.preview_path else ["--report", "x"]

        self.log.configure(state="normal"); self.log.delete("1.0", "end"); self.log.configure(state="disabled")
        self._cr_pending = False
        self.preview_lbl.config(image="", text="working…")
        self.paused = False
        self.button.set_label("PAUSE"); self.stop_btn.config(state="normal"); self.open_btn.config(state="disabled")
        self.status.config(text="on the scope…" if pull else "stacking…")
        self.set_face("focused")
        self._schedule_blink()
        threading.Thread(target=self._run, args=(cmd,), daemon=True).start()

    def _run(self, cmd):
        try:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            exe = self.scopepull[2] if self.pulling() else None
            if exe:
                # starstack.py finds scopepull with shutil.which(); make sure it can, even
                # when we found it in a pipx/Scripts folder that isn't on this PATH
                env["PATH"] = os.path.dirname(exe) + os.pathsep + env.get("PATH", "")
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, bufsize=0, creationflags=flags, env=env)
            join_job(self.job, self.proc)
        except Exception as e:
            self.q.put(("line", f"couldn't start starstack: {e}\n")); self.q.put(("done", 1)); return
        self._relay(self.proc.stdout)
        self.q.put(("done", self.proc.wait()))

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "scopepull":
                    self._scopepull_found(payload)
                elif kind == "scopepull_latest":
                    self._scopepull_latest(payload)
                elif kind == "starstack_latest":
                    self._starstack_latest(payload)
                elif kind == "install_done":
                    self._install_done(payload)
                elif kind == "line":
                    text = payload
                    tag = None
                    low = text.lower()
                    if "you're welcome" in low or "they know what they did" in low or "relax" in low or "go outside" in low or "go to bed" in low:
                        tag = "owl"
                    elif "already stacked this one" in low or "nothing new on the scope" in low:
                        tag = "owl"
                    elif ("wouldn't" in low or "failed" in low or "couldn't" in low or "error" in low
                          or "unreachable" in low or "not stacking" in low or "isn't installed" in low):
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
        if "owl is on the scope" in low:
            self.status.config(text="on the scope…")
            self.set_face("waiting")            # nothing to do but wait for the download
        elif low.startswith("whole night:") or low.startswith("[1/"):
            self.status.config(text="stacking…")
            self.set_face("focused")
        elif "already stacked this one" in low:
            self._glance("judging", "focused", 1500)
        elif low.startswith("  registering") or "reference:" in low or "debayering" in low:
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
        self.button.set_label("PULL+STACK" if self.pulling() else "STACK")
        self.button.set_enabled(True); self.stop_btn.config(state="disabled")
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

    def quit_app(self):
        """Closing the window ends the run and everything it started. Before
        0.2.13, closing left the stacker -- and a scopepull download under it
        -- running unseen; the orphan kept its .zip.partial open and every
        later pull of that observation died at the rename with "being used by
        another process". The job object (make_job) covers the case this
        handler can't: starstack.exe killed from Task Manager."""
        if self.running():
            try:
                kill_tree(self.proc)
                self.proc.wait(timeout=5)
            except Exception:
                pass
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
