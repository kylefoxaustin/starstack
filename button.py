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

HERE = os.path.dirname(os.path.abspath(__file__))
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
    """Seestar-style layout: a lights/ subfolder with frames in it."""
    for d in ("lights", "light"):
        p = os.path.join(folder, d)
        if os.path.isdir(p) and has_images(p):
            return True
    return False


def is_session(folder):
    return os.path.isdir(folder) and (has_images(folder) or is_sorted(folder))


def is_night(folder):
    """A folder of session folders rather than a folder of frames."""
    if not os.path.isdir(folder) or is_session(folder):
        return False
    return any(is_session(os.path.join(folder, d)) for d in os.listdir(folder)
               if os.path.isdir(os.path.join(folder, d)))


def target_name(folder):
    """Name the output after the target when the scope tells us."""
    mpath = os.path.join(folder, "manifest.json")
    name = None
    if os.path.exists(mpath):
        try:
            import json
            with open(mpath) as fh:
                name = json.load(fh).get("nameTarget")
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
        self.create_text(c, c - lift + 2, text="STACK", fill="#fff4ee",
                         font=("Impact", int(s * 0.17)) if sys.platform.startswith("win") else ("DejaVu Sans", int(s * 0.14), "bold"))

    def set_enabled(self, on):
        self.enabled = on
        self.draw(pressed=False)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("starstack")
        self.configure(bg=NAVY)
        self._set_icon()
        self.geometry("880x720")
        self.minsize(720, 560)
        self.proc = None
        self.q = queue.Queue()
        self.preview_path = None
        self.out_dir = None
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
        owl_path = os.path.join(HERE, "docs", "owl_256.png")
        if os.path.exists(owl_path):
            try:
                self.owl = tk.PhotoImage(file=owl_path).subsample(2, 2)
                tk.Label(top, image=self.owl, bg=NAVY).pack(side="left", padx=(0, 14))
            except tk.TclError:
                pass
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

        opts = tk.Frame(self, bg=NAVY); opts.pack(fill="x", padx=18)
        self.bits16 = tk.BooleanVar(value=True)
        self.keep_all = tk.BooleanVar(value=False)
        for text, var in (("16-bit output", self.bits16), ("keep every frame (skip the quality pass)", self.keep_all)):
            tk.Checkbutton(opts, text=text, variable=var, bg=NAVY, fg="#c9cfe0", selectcolor=NAVY2,
                           activebackground=NAVY, activeforeground=CREAM, font=SANS).pack(side="left", padx=(0, 16))

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
            n = sum(1 for d in os.listdir(f) if is_session(os.path.join(f, d)))
            self.mode_lbl.config(text=f"whole night: {n} session folders. One press does all of them.")
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
        f = self.folder.get().strip().strip('"')
        if not os.path.isdir(f):
            self.say("pick a folder first. I can't stack a feeling.\n", "bad"); return
        if is_night(f):
            self.out_dir = os.path.join(f, "stacks")
            out_arg = self.out_dir
            self.preview_path = None
        elif is_session(f):
            self.out_dir = os.path.join(f, "stacked")
            os.makedirs(self.out_dir, exist_ok=True)
            name = target_name(f)
            out_arg = os.path.join(self.out_dir, name + ".tif")
            self.preview_path = os.path.join(self.out_dir, name + "_look.png")
        else:
            self.say("no image files in that folder.\n", "bad"); return

        cmd = [os.environ.get("STARSTACK_PYTHON", sys.executable), "-u", STARSTACK, f, "-o", out_arg]
        if self.preview_path:
            cmd += ["--preview", self.preview_path]
        if self.bits16.get():
            cmd += ["--bits", "16"]
        if self.keep_all.get():
            cmd += ["--keep-all"]
        cmd += ["--report", os.path.join(self.out_dir, "frames.csv")] if self.preview_path else ["--report", "x"]

        self.log.configure(state="normal"); self.log.delete("1.0", "end"); self.log.configure(state="disabled")
        self.preview_lbl.config(image="", text="working…")
        self.button.set_enabled(False); self.stop_btn.config(state="normal"); self.open_btn.config(state="disabled")
        self.status.config(text="stacking…")
        threading.Thread(target=self._run, args=(cmd,), daemon=True).start()

    def _run(self, cmd):
        try:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1, creationflags=flags, errors="replace")
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
                else:
                    self._finished(payload)
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _finished(self, rc):
        self.button.set_enabled(True); self.stop_btn.config(state="disabled")
        self.open_btn.config(state="normal")
        if rc == 0:
            self.status.config(text="done. go outside.")
            self._show_preview()
        else:
            self.status.config(text=f"stopped (exit {rc}). read the log.")
            self.preview_lbl.config(image="", text="no picture this time")

    def _on_resize(self, _):
        if self.preview_path is None and not self.out_dir:
            return
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(200, lambda: self.button.enabled and self._show_preview())

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

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.say("\nstopped. fine.\n", "bad")


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
