#!/usr/bin/env python3
"""A small Tk interface for the Reasonable Companion pack downloader.

The downloader itself stays in :mod:`download_packs`; this file only gathers
the same options in a graphical form and streams its normal output to a log.
"""

import contextlib
import io
import os
import platform
import queue
import threading
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox

import download_packs


# Muted olive colours keep the screen calm and work well on light and dark OS
# themes alike.  Classic Tk widgets are intentional: the application has no
# third-party dependencies, just like the downloader.
COLORS = {
    "background": "#202817",
    "panel": "#303a22",
    "field": "#172012",
    "border": "#5d6b43",
    "accent": "#9caf55",
    "accent_active": "#bdcf71",
    "text": "#edf1d8",
    "muted": "#b3bd93",
    "error": "#e79278",
}


class QueueWriter(io.TextIOBase):
    """Send output from the worker thread to Tk's main thread."""

    def __init__(self, output_queue, tag):
        self.output_queue = output_queue
        self.tag = tag

    def writable(self):
        return True

    def write(self, text):
        if text:
            self.output_queue.put(("output", self.tag, text))
        return len(text)

    def flush(self):
        pass


class ReasonableCompanionApp:
    def __init__(self, root):
        self.root = root
        self.output_queue = queue.Queue()
        self.running = False
        self.system = platform.system()

        self.output_dir = tk.StringVar()
        self.wine_prefix = tk.StringVar()
        self.wine_user = tk.StringVar()
        self.start_id = tk.StringVar()
        self.end_id = tk.StringVar()
        self.auth_method = tk.StringVar(value="companion")
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.access_token = tk.StringVar()
        self.refresh_token = tk.StringVar()
        self.sync_cloud = tk.BooleanVar(value=True)
        self.verbose = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Ready. Choose an action below.")

        self._configure_root()
        self._build()
        self._show_auth_fields()
        self.root.after(80, self._drain_output)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _configure_root(self):
        self.root.title("Reasonable Companion")
        self.root.geometry("920x760")
        self.root.minsize(780, 620)
        self.root.configure(bg=COLORS["background"])
        # Tk treats a tuple such as ("TkFixedFont", 10) as a family name and
        # falls back to a proportional font, so derive from the named fonts.
        tkfont.nametofont("TkDefaultFont").configure(size=10)
        self.fixed_font = tkfont.nametofont("TkFixedFont").copy()
        self.fixed_font.configure(size=10)
        self.fixed_bold_font = self.fixed_font.copy()
        self.fixed_bold_font.configure(weight="bold")
        self.root.option_add("*Font", "TkDefaultFont")

    def _label(self, parent, text, **kwargs):
        options = {
            "text": text,
            "bg": COLORS["panel"],
            "fg": COLORS["text"],
            "anchor": "w",
        }
        options.update(kwargs)
        return tk.Label(parent, **options)

    def _section(self, parent, title):
        frame = tk.Frame(parent, bg=COLORS["panel"], highlightbackground=COLORS["border"],
                         highlightthickness=1, padx=14, pady=12)
        self._label(frame, text="[ {} ]".format(title), fg=COLORS["accent"],
                    font=self.fixed_bold_font).grid(
                        row=0, column=0, columnspan=4, sticky="w", pady=(0, 10)
                    )
        return frame

    def _entry(self, parent, variable, width=30, show=None):
        return tk.Entry(
            parent, textvariable=variable, width=width, show=show,
            bg=COLORS["field"], fg=COLORS["text"], insertbackground=COLORS["text"],
            relief="flat", highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"], highlightthickness=1,
        )

    def _button(self, parent, text, command, primary=False, **kwargs):
        color = COLORS["accent"] if primary else COLORS["panel"]
        foreground = COLORS["background"] if primary else COLORS["text"]
        return tk.Button(
            parent, text=text, command=command, bg=color, fg=foreground,
            activebackground=COLORS["accent_active"], activeforeground=COLORS["background"],
            relief="flat", bd=0, padx=12, pady=7, cursor="hand2", **kwargs
        )

    def _check(self, parent, text, variable):
        return tk.Checkbutton(
            parent, text=text, variable=variable, bg=COLORS["panel"], fg=COLORS["text"],
            activebackground=COLORS["panel"], activeforeground=COLORS["text"],
            selectcolor=COLORS["field"], highlightthickness=0,
        )

    def _build(self):
        outer = tk.Frame(self.root, bg=COLORS["background"], padx=20, pady=18)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=COLORS["background"])
        header.pack(fill="x", pady=(0, 14))
        tk.Label(
            header,
            text="+----------------------------------------------------------+\n"
                 "|  REASONABLE COMPANION  ::  REASON SOUND PACK INSTALLER   |\n"
                 "+----------------------------------------------------------+",
            justify="left", anchor="w", bg=COLORS["background"], fg=COLORS["accent"],
            font=self.fixed_bold_font,
        ).pack(anchor="w")
        tk.Label(
            header, text="Find, review, and install the packs your account can use.",
            bg=COLORS["background"], fg=COLORS["muted"], anchor="w",
        ).pack(anchor="w", pady=(6, 0))
        tk.Label(
            header, text="Detected platform: {}".format(self._platform_name()),
            bg=COLORS["background"], fg=COLORS["muted"], anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        form = tk.Frame(outer, bg=COLORS["background"])
        form.pack(fill="x")
        form.columnconfigure(0, weight=1, uniform="form")
        form.columnconfigure(1, weight=1, uniform="form")

        location = self._section(form, "LOCATION")
        location.grid(row=0, column=0, sticky="nsew", padx=(0, 7), pady=(0, 10))
        location.columnconfigure(1, weight=1)
        self._label(location, "Pack folder (optional)").grid(row=1, column=0, sticky="w", pady=4)
        self._entry(location, self.output_dir).grid(row=1, column=1, sticky="ew", padx=(10, 6), pady=4)
        self._button(location, "Browse", self._choose_output).grid(row=1, column=2, pady=4)
        if self.system == "Linux":
            self._label(location, "Wine / Bottles prefix").grid(row=2, column=0, sticky="w", pady=4)
            self._entry(location, self.wine_prefix).grid(row=2, column=1, sticky="ew", padx=(10, 6), pady=4)
            self._button(location, "Browse", self._choose_prefix).grid(row=2, column=2, pady=4)
            self._label(location, "Wine user (optional)").grid(row=3, column=0, sticky="w", pady=4)
            self._entry(location, self.wine_user).grid(row=3, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=4)
            location_help = (
                "Leave the pack folder blank to find Reason Companion in your "
                "usual Wine or Bottles prefixes. Choose a prefix when needed."
            )
        else:
            location_help = (
                "Leave the pack folder blank to use Reason Companion's Packs "
                "folder for this {} account.".format(self._platform_name())
            )
        self._label(
            location, location_help,
            fg=COLORS["muted"], wraplength=390, justify="left",
        ).grid(row=4 if self.system == "Linux" else 2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        selection = self._section(form, "PACKS")
        selection.grid(row=0, column=1, sticky="nsew", padx=(7, 0), pady=(0, 10))
        selection.columnconfigure(1, weight=1)
        self._label(selection, "First pack ID").grid(row=1, column=0, sticky="w", pady=4)
        self._entry(selection, self.start_id, width=14).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=4)
        self._label(selection, "Last pack ID").grid(row=2, column=0, sticky="w", pady=4)
        self._entry(selection, self.end_id, width=14).grid(row=2, column=1, sticky="w", padx=(10, 0), pady=4)
        self._label(
            selection, "Leave both blank for every released pack. The limits are inclusive.",
            fg=COLORS["muted"], wraplength=350, justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 5))
        self._check(selection, "Register completed installs with cloud sync", self.sync_cloud).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=2
        )
        self._check(selection, "Show download progress and warnings", self.verbose).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=2
        )

        auth = self._section(form, "SIGN IN")
        auth.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        for column in range(3):
            auth.columnconfigure(column, weight=1 if column == 2 else 0)
        choices = tk.Frame(auth, bg=COLORS["panel"])
        choices.grid(row=1, column=0, columnspan=3, sticky="w")
        for value, label in (
            ("companion", "Use Companion session"),
            ("login", "Sign in here"),
            ("token", "Use access token"),
        ):
            tk.Radiobutton(
                choices, text=label, value=value, variable=self.auth_method,
                command=self._show_auth_fields, bg=COLORS["panel"], fg=COLORS["text"],
                activebackground=COLORS["panel"], activeforeground=COLORS["text"],
                selectcolor=COLORS["field"], highlightthickness=0,
            ).pack(side="left", padx=(0, 18))
        self.auth_detail = tk.Frame(auth, bg=COLORS["panel"])
        self.auth_detail.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(9, 0))
        self.auth_detail.columnconfigure(1, weight=1)

        action = tk.Frame(outer, bg=COLORS["background"])
        action.pack(fill="x", pady=(0, 10))
        self.scan_button = self._button(action, "[ Scan / Dry run ]", self._scan)
        self.scan_button.pack(side="left")
        self.install_button = self._button(action, "[ Install packs ]", self._install, primary=True)
        self.install_button.pack(side="left", padx=(8, 0))
        tk.Label(
            action, textvariable=self.status, bg=COLORS["background"], fg=COLORS["muted"],
            anchor="e",
        ).pack(side="right", fill="x", expand=True)

        log_frame = self._section(outer, "ACTIVITY")
        log_frame.pack(fill="both", expand=True)
        log_frame.columnconfigure(0, weight=1)
        self._label(
            log_frame, "Scan writes nothing. Install skips packs that already exist, so it is safe to run again.",
            fg=COLORS["muted"], wraplength=820,
        ).grid(row=1, column=0, sticky="w", pady=(0, 8))
        log_box = tk.Frame(log_frame, bg=COLORS["border"])
        log_box.grid(row=2, column=0, sticky="nsew")
        log_frame.rowconfigure(2, weight=1)
        self.log = tk.Text(
            log_box, height=13, wrap="word", state="disabled", bd=0,
            bg=COLORS["field"], fg=COLORS["text"], insertbackground=COLORS["text"],
            font=self.fixed_font, padx=10, pady=9,
        )
        scroll = tk.Scrollbar(log_box, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        scroll.pack(side="right", fill="y")
        self.log.tag_configure("stderr", foreground=COLORS["error"])
        self.log.tag_configure("system", foreground=COLORS["accent"])
        self._write_log("Ready. Scan to list packs, then install when you are happy with the target.\n", "system")

    def _show_auth_fields(self):
        for child in self.auth_detail.winfo_children():
            child.destroy()
        method = self.auth_method.get()
        if method == "companion":
            self._label(
                self.auth_detail,
                "Reads the current Companion session without renewing or changing it. Open Companion and sign in first.",
                fg=COLORS["muted"], wraplength=790, justify="left",
            ).grid(row=0, column=0, sticky="w")
        elif method == "login":
            self._label(self.auth_detail, "Email / username").grid(row=0, column=0, sticky="w", pady=3)
            self._entry(self.auth_detail, self.username).grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=3)
            self._label(self.auth_detail, "Password").grid(row=1, column=0, sticky="w", pady=3)
            self._entry(self.auth_detail, self.password, show="*").grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=3)
        else:
            self._label(self.auth_detail, "Access token").grid(row=0, column=0, sticky="w", pady=3)
            self._entry(self.auth_detail, self.access_token).grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=3)
            self._label(self.auth_detail, "Refresh token (optional)").grid(row=1, column=0, sticky="w", pady=3)
            self._entry(self.auth_detail, self.refresh_token).grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=3)

    def _platform_name(self):
        return {
            "Darwin": "macOS",
            "Windows": "Windows",
            "Linux": "Linux (Wine / Bottles)",
        }.get(self.system, self.system or "unknown OS")

    def _choose_output(self):
        choice = filedialog.askdirectory(title="Choose a pack folder")
        if choice:
            self.output_dir.set(choice)

    def _choose_prefix(self):
        choice = filedialog.askdirectory(title="Choose a Wine or Bottles prefix")
        if choice:
            self.wine_prefix.set(choice)

    def _arguments(self, dry_run):
        """Build the command-line arguments after friendly form validation."""
        args = []
        start = self.start_id.get().strip()
        end = self.end_id.get().strip()
        for value, option in ((start, "--start"), (end, "--end")):
            if value:
                if not value.isdigit() or int(value) < 1:
                    raise ValueError("{} must be a positive whole number.".format(option[2:].replace("-", " ").title()))
                args.extend((option, value))
        if start and end and int(start) > int(end):
            raise ValueError("First pack ID cannot be greater than last pack ID.")

        if self.output_dir.get().strip():
            args.extend(("--output", os.path.expanduser(self.output_dir.get().strip())))
        if self.wine_prefix.get().strip():
            args.extend(("--wine-prefix", os.path.expanduser(self.wine_prefix.get().strip())))
        if self.wine_user.get().strip():
            args.extend(("--wine-user", self.wine_user.get().strip()))
        if not self.sync_cloud.get():
            args.append("--no-sync")
        if self.verbose.get():
            args.append("--verbose")
        if dry_run:
            args.append("--dry-run")

        method = self.auth_method.get()
        if method == "login":
            username = self.username.get().strip()
            password = self.password.get()
            if not username or not password:
                raise ValueError("Enter both an email / username and password to sign in here.")
            args.extend(("--login", "--username", username, "--password", password))
        elif method == "token":
            token = self.access_token.get().strip()
            refresh = self.refresh_token.get().strip()
            if not token and not refresh:
                raise ValueError("Enter an access token or a refresh token.")
            if token:
                args.extend(("--token", token))
            if refresh:
                args.extend(("--refresh-token", refresh))
        return args

    def _scan(self):
        self._start(True)

    def _install(self):
        self._start(False)

    def _start(self, dry_run):
        if self.running:
            return
        try:
            args = self._arguments(dry_run)
        except ValueError as error:
            messagebox.showerror("Check the form", str(error), parent=self.root)
            return

        self.running = True
        self.scan_button.configure(state="disabled")
        self.install_button.configure(state="disabled")
        action = "Scanning catalog" if dry_run else "Installing packs"
        self.status.set(action + "...")
        self._write_log("\n--- {} ---\n".format(action), "system")
        worker = threading.Thread(target=self._run, args=(args,), daemon=True)
        worker.start()

    def _run(self, args):
        try:
            with contextlib.redirect_stdout(QueueWriter(self.output_queue, "stdout")), \
                    contextlib.redirect_stderr(QueueWriter(self.output_queue, "stderr")):
                result = download_packs.main(args)
            self.output_queue.put(("finished", result, None))
        except BaseException as error:
            # This should only be a programming-level failure; normal download
            # errors are already turned into a readable return code by main().
            self.output_queue.put(("crashed", error, None))

    def _drain_output(self):
        try:
            while True:
                kind, value, extra = self.output_queue.get_nowait()
                if kind == "output":
                    self._write_log(extra, "stderr" if value == "stderr" else None)
                elif kind == "finished":
                    self._finish(value)
                elif kind == "crashed":
                    self._write_log("Unexpected error: {}\n".format(value), "stderr")
                    self._finish(1)
        except queue.Empty:
            pass
        self.root.after(80, self._drain_output)

    def _finish(self, result):
        self.running = False
        self.scan_button.configure(state="normal")
        self.install_button.configure(state="normal")
        if result:
            self.status.set("Finished with an error. See activity below.")
        else:
            self.status.set("Finished successfully.")

    def _write_log(self, text, tag=None):
        self.log.configure(state="normal")
        for index, piece in enumerate(text.split("\r")):
            if index:
                # A carriage return rewrites the current line (download progress).
                self.log.delete("end-1c linestart", "end-1c")
            if piece:
                self.log.insert("end", piece, tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _close(self):
        if self.running:
            messagebox.showwarning(
                "Install still running",
                "Wait for the current scan or install to finish before closing this window.",
                parent=self.root,
            )
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    ReasonableCompanionApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
