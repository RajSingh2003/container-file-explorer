"""
first_run.py
------------
A one-time setup wizard shown the first time the app launches, so a
non-technical end user isn't expected to read a README or know what
"Docker" or "oc" even are before the app is useful to them.

Checks whether the `docker` and `oc` CLIs are on PATH (the same check
backends.py already does at call time) and shows the result as a plain
checklist, with a direct download link for anything missing. The user
can continue regardless - Docker-only or OpenShift-only usage is fine,
this is purely informational so nothing is a surprise later.

Whether the wizard has already been shown is tracked with a small
marker file in the same per-user config folder the rest of the app
would use:
    ~/.container_explorer/setup_complete      (Linux/macOS)
    C:\\Users\\<you>\\.container_explorer\\setup_complete   (Windows)
"""

from __future__ import annotations

import os
import shutil
import tkinter as tk
import webbrowser
from tkinter import ttk

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".container_explorer")
MARKER_FILE = os.path.join(CONFIG_DIR, "setup_complete")

DOCKER_INSTALL_URL = "https://docs.docker.com/engine/install/"
OC_INSTALL_URL = "https://docs.openshift.com/container-platform/latest/cli_reference/openshift_cli/getting-started-cli.html"


def is_first_run() -> bool:
    return not os.path.exists(MARKER_FILE)


def mark_setup_complete() -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(MARKER_FILE, "w", encoding="utf-8") as fh:
        fh.write("done\n")


def _check_row(parent, label: str, found: bool, install_url: str, row: int):
    icon = "✅" if found else "❌"
    status = "Found on PATH" if found else "Not found"
    ttk.Label(parent, text=icon, font=("Segoe UI", 12)).grid(row=row, column=0, padx=(0, 8), pady=4, sticky="w")
    ttk.Label(parent, text=label, font=("Segoe UI", 10, "bold")).grid(row=row, column=1, sticky="w", pady=4)
    ttk.Label(parent, text=status, foreground=("#2e7d32" if found else "#c62828")).grid(
        row=row, column=2, padx=(8, 8), sticky="w", pady=4
    )
    if not found:
        ttk.Button(
            parent, text="Download...", command=lambda: webbrowser.open(install_url)
        ).grid(row=row, column=3, pady=4)


class SetupWizard(tk.Toplevel):
    """Modal first-run dialog. Call `SetupWizard(root).show()` - blocks
    (via grab_set + wait_window) until the user clicks Continue, then
    returns normally so the caller can proceed to build the main UI."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Welcome - Container File System Explorer")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_continue)  # closing = same as Continue

        outer = ttk.Frame(self, padding=20)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer, text="Welcome to Container File System Explorer",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="Let's check what's on this machine before you get started.\n"
                 "You can still continue even if something below is missing -\n"
                 "you just won't be able to use that part until it's installed.",
            justify="left",
        ).pack(anchor="w", pady=(4, 16))

        grid = ttk.Frame(outer)
        grid.pack(fill="x")

        docker_found = shutil.which("docker") is not None
        oc_found = shutil.which("oc") is not None

        _check_row(grid, "Docker CLI  (for the Local Docker tab)", docker_found, DOCKER_INSTALL_URL, row=0)
        _check_row(grid, "oc CLI  (for the OpenShift tab)", oc_found, OC_INSTALL_URL, row=1)

        if docker_found and oc_found:
            note = "Everything's ready - you're all set."
        elif docker_found or oc_found:
            note = "You can start now; install the missing one later if you need that tab."
        else:
            note = "You can still explore the app, but install at least one of these to browse a container."
        ttk.Label(outer, text=note, foreground="#555").pack(anchor="w", pady=(14, 0))

        btn_row = ttk.Frame(outer)
        btn_row.pack(fill="x", pady=(18, 0))
        ttk.Button(btn_row, text="Continue", command=self._on_continue).pack(side="right")

        self.update_idletasks()
        self._center_on_parent(master)

    def _center_on_parent(self, master):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = master.winfo_x() + (master.winfo_width() - w) // 2
        y = master.winfo_y() + (master.winfo_height() - h) // 2
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _on_continue(self):
        mark_setup_complete()
        self.destroy()

    def show(self):
        """Block until the user dismisses the wizard."""
        self.grab_set()
        self.wait_window(self)
