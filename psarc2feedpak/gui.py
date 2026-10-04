"""A small tkinter front-end for the converter.

Pick one or more .psarc files, pick an output folder, hit Convert. Conversion
runs on a worker thread; log lines are pushed back to the UI through a queue so
the window stays responsive.
"""

import ctypes
import json
import queue
import re
import sys
import threading
import tkinter as tk
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import filedialog, font as tkfont, ttk

from . import __version__, settings
from .audio import Tools
from .convert import convert, ConversionError
from .settings import Options, render_name

APP_TITLE = "psarc2feedpak"
RELEASES_API = ("https://api.github.com/repos/"
                "carelesshangman/psarc2feedpak/releases/latest")
RELEASES_PAGE = "https://github.com/carelesshangman/psarc2feedpak/releases"

# Palette. One dark theme, warm accent.
BG = "#15171c"          # window
SURFACE = "#1e222a"     # buttons, banner chrome
SURFACE_HI = "#272c36"  # hovered buttons
SUNKEN = "#101216"      # list + log wells
BORDER = "#2b303b"
TEXT = "#e8eaf0"
MUTED = "#98a0b0"
LOG_FG = "#c4cad7"
ACCENT = "#ff9350"
ACCENT_HI = "#ffa76e"
ACCENT_LO = "#e97f3c"
ON_ACCENT = "#221204"
GREEN = "#7fd696"
RED = "#ff8383"
AMBER = "#ffd88a"
AMBER_BG = "#332b16"
LINK = "#82b6ff"


def _version_tuple(s):
    nums = re.findall(r"\d+", s or "")
    return tuple(int(n) for n in nums) if nums else (0,)


def _mono_family(root):
    fams = set(tkfont.families(root))
    for name in ("Cascadia Mono", "Consolas", "Courier New"):
        if name in fams:
            return name
    return "TkFixedFont"


def _dark_title_bar(root):
    """Ask DWM for a dark title bar on Windows. Harmless anywhere else."""
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(  # 20 = USE_IMMERSIVE_DARK_MODE
            hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def _init_style(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(background=BG)

    base = ("Segoe UI", 10)
    style.configure(".", background=BG, foreground=TEXT, font=base,
                    bordercolor=BORDER, focuscolor=ACCENT)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Title.TLabel", font=("Segoe UI Semibold", 16))
    style.configure("Section.TLabel", foreground=MUTED,
                    font=("Segoe UI", 9, "bold"))
    style.configure("Status.TLabel", foreground=MUTED)
    style.configure("Done.TLabel", foreground=GREEN)

    style.configure("TButton", background=SURFACE, foreground=TEXT,
                    borderwidth=0, focusthickness=0, padding=(14, 7))
    style.map("TButton",
              background=[("disabled", SURFACE), ("pressed", BORDER),
                          ("active", SURFACE_HI)],
              foreground=[("disabled", "#5d6472")])

    style.configure("Accent.TButton", background=ACCENT, foreground=ON_ACCENT,
                    font=("Segoe UI Semibold", 10), padding=(22, 8))
    style.map("Accent.TButton",
              background=[("disabled", SURFACE), ("pressed", ACCENT_LO),
                          ("active", ACCENT_HI)],
              foreground=[("disabled", "#5d6472")])

    style.configure("Horizontal.TProgressbar", troughcolor=SUNKEN,
                    background=ACCENT, bordercolor=BG,
                    lightcolor=ACCENT, darkcolor=ACCENT, thickness=4)

    style.configure("Vertical.TScrollbar", background=SURFACE_HI,
                    troughcolor=SUNKEN, bordercolor=SUNKEN,
                    arrowcolor=MUTED, gripcount=0, arrowsize=12)
    style.map("Vertical.TScrollbar",
              background=[("active", "#39404e"), ("pressed", "#39404e")])

    style.configure("Hint.TLabel", foreground=MUTED, font=("Segoe UI", 8))
    style.configure("Error.TLabel", foreground=RED, font=("Segoe UI", 9))

    style.configure("TCheckbutton", background=BG, foreground=TEXT,
                    indicatorcolor=SUNKEN, focuscolor=BG, padding=(0, 3))
    style.map("TCheckbutton",
              background=[("active", BG)],
              indicatorcolor=[("selected", ACCENT), ("pressed", ACCENT)],
              foreground=[("disabled", "#5d6472")])

    field = dict(fieldbackground=SUNKEN, background=SURFACE, foreground=TEXT,
                 bordercolor=BORDER, lightcolor=SUNKEN, darkcolor=SUNKEN,
                 arrowcolor=MUTED, insertcolor=TEXT)
    style.configure("TCombobox", **field)
    style.configure("TSpinbox", **field)
    style.map("TCombobox", fieldbackground=[("readonly", SUNKEN)])
    style.map("TSpinbox", fieldbackground=[("disabled", BG)],
              foreground=[("disabled", "#5d6472")])
    root.option_add("*TCombobox*Listbox.background", SUNKEN)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", SURFACE_HI)
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)


def _well(parent):
    """A bordered dark 'card' the list and log sit in."""
    return tk.Frame(parent, background=SUNKEN, highlightthickness=1,
                    highlightbackground=BORDER, highlightcolor=BORDER)


class App:
    def __init__(self, root):
        self.root = root
        self.paths = []
        self.outdir = tk.StringVar(value="Same folder as each song")
        self.msgs = queue.Queue()
        self.busy = False
        self.options = settings.load()
        self._settings_win = None

        root.title(APP_TITLE)
        root.minsize(620, 540)
        root.geometry("720x580")
        _init_style(root)
        _dark_title_bar(root)
        mono = _mono_family(root)

        # --- header ---
        header = ttk.Frame(root)
        header.pack(fill="x", padx=16, pady=(14, 10))
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel"
                  ).pack(side="left")
        ttk.Label(header, text=f"v{__version__}", style="Muted.TLabel"
                  ).pack(side="left", padx=(9, 0), pady=(7, 0))
        ttk.Button(header, text="⚙  Settings", command=self.open_settings
                   ).pack(side="right")
        ttk.Label(header, text="Rocksmith .psarc  →  feedpak",
                  style="Muted.TLabel").pack(side="right", padx=(0, 14),
                                             pady=(7, 0))

        # --- song list ---
        row = ttk.Frame(root)
        row.pack(fill="x", padx=16)
        ttk.Label(row, text="SONGS", style="Section.TLabel").pack(side="left")
        self.count_lbl = ttk.Label(row, text="", style="Muted.TLabel")
        self.count_lbl.pack(side="right")

        well = _well(root)
        well.pack(fill="x", padx=16, pady=(4, 8))
        list_font = ("Segoe UI", 10)
        self.filelist = tk.Listbox(
            well, height=5, activestyle="none", borderwidth=0,
            highlightthickness=0, background=SUNKEN, foreground=TEXT,
            selectbackground=SURFACE_HI, selectforeground=TEXT,
            selectmode="extended", font=list_font)
        self.filelist.pack(fill="both", expand=True, padx=8, pady=6)
        self.filelist.bind("<Delete>", lambda e: self.remove_selected())
        self.placeholder = tk.Label(
            well, text="No songs yet. Click “Add .psarc files” to pick your CDLC.",
            background=SUNKEN, foreground=MUTED, font=list_font)
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")

        btns = ttk.Frame(root)
        btns.pack(fill="x", padx=16)
        ttk.Button(btns, text="Add .psarc files", command=self.pick_files
                   ).pack(side="left")
        ttk.Button(btns, text="Add folder", command=self.pick_folder
                   ).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Remove selected", command=self.remove_selected
                   ).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Clear", command=self.clear_files
                   ).pack(side="left", padx=(8, 0))
        out_btn = ttk.Button(btns, text="Output folder", command=self.pick_outdir)
        out_btn.pack(side="right")
        ttk.Label(btns, textvariable=self.outdir, style="Muted.TLabel"
                  ).pack(side="right", padx=(0, 10))

        # --- log ---
        ttk.Label(root, text="LOG", style="Section.TLabel"
                  ).pack(anchor="w", padx=16, pady=(12, 0))
        well = _well(root)
        well.pack(fill="both", expand=True, padx=16, pady=(4, 10))
        self.log = tk.Text(well, height=10, wrap="word", state="disabled",
                           font=(mono, 9), background=SUNKEN,
                           foreground=LOG_FG, borderwidth=0,
                           highlightthickness=0, insertbackground=TEXT,
                           selectbackground=SURFACE_HI, padx=10, pady=8)
        sb = ttk.Scrollbar(well, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 1), pady=1)
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("ok", foreground=GREEN)
        self.log.tag_configure("err", foreground=RED)
        self.log.tag_configure("warn", foreground=AMBER)
        self.log.tag_configure("head", foreground=TEXT,
                               font=(mono, 9, "bold"))

        # --- footer ---
        self.progress = ttk.Progressbar(root, mode="determinate", maximum=1)
        self.progress.pack(fill="x", padx=16)
        bottom = ttk.Frame(root)
        bottom.pack(fill="x", padx=16, pady=(8, 14))
        self.convert_btn = ttk.Button(bottom, text="Convert", state="disabled",
                                      style="Accent.TButton", command=self.start)
        self.convert_btn.pack(side="right")
        self.status = ttk.Label(bottom, text="Ready", style="Status.TLabel")
        self.status.pack(side="left")

        self._refresh_count()
        self._check_tools()
        threading.Thread(target=self._check_update, daemon=True).start()
        self.root.after(100, self._drain)

    # --- tool check --------------------------------------------------------
    def _check_tools(self):
        missing = Tools().missing()
        if missing:
            self._write("Heads up: " + " and ".join(missing) + " not found. "
                        "Charts will still convert, but audio won't. "
                        "See the README for how to add them.\n")

    # --- update check ------------------------------------------------------
    def _check_update(self):
        try:
            req = urllib.request.Request(
                RELEASES_API, headers={"User-Agent": "psarc2feedpak"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                latest = json.load(resp).get("tag_name", "")
        except Exception:
            return  # offline or rate-limited; never bother the user about it
        if _version_tuple(latest) > _version_tuple(__version__):
            self.msgs.put(("update", latest))

    def _show_update(self, latest):
        bar = tk.Frame(self.root, background=AMBER_BG)
        bar.pack(fill="x", padx=16, pady=(0, 8), before=self.progress)
        tk.Label(bar, background=AMBER_BG, foreground=AMBER,
                 font=("Segoe UI", 9),
                 text=f"Update available: {latest} (you have v{__version__})."
                 ).pack(side="left", padx=(10, 4), pady=5)
        link = tk.Label(bar, text="Download", background=AMBER_BG,
                        foreground=LINK, cursor="hand2", font=("Segoe UI", 9))
        f = tkfont.Font(link, link.cget("font"))
        f.configure(underline=True)
        link.configure(font=f)
        link.pack(side="left", pady=5)
        link.bind("<Button-1>", lambda e: webbrowser.open(RELEASES_PAGE))
        close = tk.Label(bar, text="✕", background=AMBER_BG, foreground=AMBER,
                         cursor="hand2")
        close.pack(side="right", padx=10)
        close.bind("<Button-1>", lambda e: bar.destroy())

    # --- settings ----------------------------------------------------------
    def open_settings(self):
        if self._settings_win and self._settings_win.winfo_exists():
            self._settings_win.lift()
            return
        o = self.options
        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.title("Settings")
        win.configure(background=BG)
        win.transient(self.root)
        win.resizable(False, False)
        _dark_title_bar(win)

        v_tpl = tk.StringVar(value=o.name_template)
        v_over = tk.BooleanVar(value=o.overwrite)
        v_keep = tk.BooleanVar(value=o.keep_dir)
        v_audio = tk.BooleanVar(value=o.include_audio)
        v_q = tk.IntVar(value=o.audio_quality)
        v_cover = tk.BooleanVar(value=o.include_cover)
        v_ladder = tk.BooleanVar(value=o.include_phrases)

        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=20, pady=16)
        body.columnconfigure(1, weight=1)
        r = 0

        ttk.Label(body, text="OUTPUT", style="Section.TLabel"
                  ).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        ttk.Label(body, text="Filename"
                  ).grid(row=r, column=0, sticky="w", pady=(6, 0))
        tpl = ttk.Combobox(body, textvariable=v_tpl, width=32, values=(
            "{artist} - {title}", "{title}", "{title} - {artist}",
            "{artist} - {title} ({year})"))
        tpl.grid(row=r, column=1, sticky="we", padx=(12, 0), pady=(6, 0)); r += 1
        ttk.Label(body, style="Hint.TLabel",
                  text="Fields: {artist} {title} {album} {year}. "
                       "\".feedpak\" is added for you."
                  ).grid(row=r, column=1, sticky="w", padx=(12, 0)); r += 1
        ttk.Checkbutton(body, text="Overwrite a .feedpak that already exists",
                        variable=v_over
                        ).grid(row=r, column=0, columnspan=2, sticky="w",
                               pady=(8, 0)); r += 1
        ttk.Checkbutton(body, text="Keep the unzipped .feedpak.dir folder too",
                        variable=v_keep
                        ).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1

        ttk.Label(body, text="WHAT GOES IN", style="Section.TLabel"
                  ).grid(row=r, column=0, columnspan=2, sticky="w",
                         pady=(16, 2)); r += 1
        audio_row = ttk.Frame(body)
        audio_row.grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        q_spin = ttk.Spinbox(audio_row, from_=0, to=10, width=4,
                             textvariable=v_q)
        ttk.Checkbutton(
            audio_row, text="Audio, at Vorbis quality", variable=v_audio,
            command=lambda: q_spin.config(
                state="normal" if v_audio.get() else "disabled")
        ).pack(side="left")
        q_spin.pack(side="left", padx=(8, 0))
        if not o.include_audio:
            q_spin.config(state="disabled")
        ttk.Label(audio_row, text="0 = smallest, 10 = best, 5 = default",
                  style="Hint.TLabel").pack(side="left", padx=(10, 0))
        ttk.Checkbutton(body, text="Cover art", variable=v_cover
                        ).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        ttk.Checkbutton(body,
                        text="Per-phrase difficulty ladder (adaptive difficulty)",
                        variable=v_ladder
                        ).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1

        err = ttk.Label(body, text="", style="Error.TLabel")
        err.grid(row=r, column=0, columnspan=2, sticky="w", pady=(10, 0)); r += 1

        def restore():
            d = Options()
            v_tpl.set(d.name_template)
            v_over.set(d.overwrite)
            v_keep.set(d.keep_dir)
            v_audio.set(d.include_audio)
            v_q.set(d.audio_quality)
            v_cover.set(d.include_cover)
            v_ladder.set(d.include_phrases)
            q_spin.config(state="normal")
            err.config(text="")

        def do_save():
            tpl_s = v_tpl.get().strip()
            try:
                sample = render_name(tpl_s, artist="a", title="t",
                                     album="b", year=2014)
            except ValueError:
                sample = ""
            if not sample:
                err.config(text="That filename template doesn't work. "
                                "Try {artist} - {title}.")
                return
            try:
                quality = max(0, min(10, int(v_q.get())))
            except (tk.TclError, ValueError):
                quality = Options().audio_quality
            self.options = Options(
                name_template=tpl_s, audio_quality=quality,
                include_audio=v_audio.get(), include_cover=v_cover.get(),
                include_phrases=v_ladder.get(), keep_dir=v_keep.get(),
                overwrite=v_over.get())
            settings.save(self.options)
            win.destroy()

        btns = ttk.Frame(body)
        btns.grid(row=r, column=0, columnspan=2, sticky="we", pady=(14, 0))
        ttk.Button(btns, text="Restore defaults", command=restore
                   ).pack(side="left")
        ttk.Button(btns, text="Save", style="Accent.TButton", command=do_save
                   ).pack(side="right")
        ttk.Button(btns, text="Cancel", command=win.destroy
                   ).pack(side="right", padx=(0, 8))

        win.grab_set()
        tpl.focus_set()

    # --- file selection ----------------------------------------------------
    def pick_files(self):
        picked = filedialog.askopenfilenames(
            title="Choose Rocksmith .psarc files",
            filetypes=[("Rocksmith CDLC", "*.psarc"), ("All files", "*.*")])
        self._add_paths(picked)

    def pick_folder(self):
        d = filedialog.askdirectory(title="Choose a folder with .psarc files")
        if d:
            # Whole tree, any depth; case-insensitive extension match.
            self._add_paths(sorted(f.as_posix() for f in Path(d).rglob("*")
                                   if f.is_file()
                                   and f.suffix.lower() == ".psarc"))

    def _add_paths(self, picked):
        for p in picked:
            if p not in self.paths:
                self.paths.append(p)
                self.filelist.insert("end", f"  {Path(p).name}")
        self._refresh_count()

    def remove_selected(self):
        for i in reversed(self.filelist.curselection()):
            self.filelist.delete(i)
            del self.paths[i]
        self._refresh_count()

    def clear_files(self):
        self.paths.clear()
        self.filelist.delete(0, "end")
        self._refresh_count()

    def pick_outdir(self):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.outdir.set(d)

    def _refresh_count(self):
        n = len(self.paths)
        self.count_lbl.config(
            text="" if n == 0 else f"{n} file{'s' if n != 1 else ''}")
        if n == 0:
            self.placeholder.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.placeholder.place_forget()
        if not self.busy:
            self.convert_btn.config(state="normal" if n else "disabled")

    # --- conversion --------------------------------------------------------
    def start(self):
        if self.busy or not self.paths:
            return
        self.busy = True
        self.convert_btn.config(state="disabled")
        self.status.config(text="Converting…", style="Status.TLabel")
        self.progress.config(maximum=len(self.paths), value=0)
        files = list(self.paths)
        outdir = self.outdir.get()
        out = None if outdir.startswith("Same folder") else outdir
        threading.Thread(target=self._work, args=(files, out, self.options),
                         daemon=True).start()

    def _work(self, files, outdir, options):
        ok = 0
        for i, src in enumerate(files, 1):
            src = Path(src)
            self.msgs.put(("progress", (i - 1, len(files))))
            self.msgs.put(("log", f"\n[{i}/{len(files)}] {src.name}\n"))
            try:
                convert(src, out_dir=outdir, options=options,
                        log=lambda m: self.msgs.put(("log", m + "\n")))
                ok += 1
            except ConversionError as e:
                self.msgs.put(("log", f"  FAILED: {e}\n"))
            except Exception as e:  # unexpected — show it rather than dying silently
                self.msgs.put(("log", f"  ERROR: {e}\n"))
        self.msgs.put(("done", f"Done — {ok}/{len(files)} converted."))

    # --- queue pump --------------------------------------------------------
    def _drain(self):
        try:
            while True:
                kind, payload = self.msgs.get_nowait()
                if kind == "log":
                    self._write(payload)
                elif kind == "update":
                    self._show_update(payload)
                elif kind == "progress":
                    done, total = payload
                    self.progress.config(maximum=total, value=done)
                    self.status.config(text=f"Converting {done + 1} of {total}…")
                elif kind == "done":
                    self.busy = False
                    self.progress.config(value=self.progress["maximum"])
                    self.status.config(text=payload, style="Done.TLabel")
                    self._refresh_count()
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    @staticmethod
    def _line_tag(line):
        if "FAILED" in line or "ERROR" in line:
            return "err"
        if (line.lstrip().startswith("warning:") or line.startswith("Heads up")
                or line.startswith("Skipped:")):
            return "warn"
        if line.startswith("Wrote "):
            return "ok"
        if re.match(r"\[\d+/\d+\]", line) or line.startswith("Converting "):
            return "head"
        return None

    def _write(self, text):
        self.log.config(state="normal")
        for line in text.splitlines(keepends=True):
            tag = self._line_tag(line)
            self.log.insert("end", line, tag or ())
        self.log.see("end")
        self.log.config(state="disabled")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
