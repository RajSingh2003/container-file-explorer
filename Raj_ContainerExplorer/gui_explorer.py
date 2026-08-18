#!/usr/bin/env python3
"""
gui_explorer.py
----------------
Task 4  - Graphical UI for Container File System (Docker)
Task 5  - File Copy Between Container and Local Machine
Task 7  - Enhance UI for OpenShift Pod and Container Navigation

A single desktop application (tkinter - built in, no install needed) with
two tabs:

  * "Local Docker"  - browse a running container's file system, copy
                       files in both directions.
  * "OpenShift"     - pick Namespace -> Pod -> Container, browse that
                       container's file system, copy files in both
                       directions. Handles multi-container pods.

Each tab has three parts, top to bottom:
  1. Navigation controls  (container dropdown, or namespace/pod/container)
  2. Preview panel        - key details of whatever's currently selected
                             (image, status, restart count, etc.)
  3. File tree            - lazy-loading browser + Copy/View Logs buttons

Below both tabs, a shared Activity Log panel timestamps every action
(selection changes, copy attempts, errors) directly in the window -
there is no log file and nothing is printed to the terminal; everything
observable happens in the UI itself.

All docker/oc calls run on a background thread (via `threading`) so the
UI never freezes, using a small work-queue processed with `root.after()`
to marshal results back onto the Tk main thread (Tk is not thread-safe,
so widgets must only be touched from the main thread).

Run with:
    python gui_explorer.py
"""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from typing import Callable, Optional

from backends import DockerBackend, OpenShiftBackend, BackendError, FileEntry, TargetPreview
from version import __version__ as APP_VERSION
from first_run import SetupWizard, is_first_run
from update_checker import check_for_update, RELEASES_PAGE_URL
import webbrowser

PLACEHOLDER = "__loading__"


def run_in_background(fn: Callable, on_done: Callable, on_error: Callable):
    """Run `fn()` on a worker thread; marshal the result/exception back to
    the caller via `on_done`/`on_error`, which are expected to already be
    safe to call from a Tk callback (see BrowserPanel._poll_queue)."""

    def worker():
        try:
            result = fn()
            on_done(result)
        except BackendError as exc:
            on_error(str(exc))
        except Exception as exc:  # noqa: BLE001 - last-resort safety net for the GUI
            on_error(f"Unexpected error: {exc}")

    threading.Thread(target=worker, daemon=True).start()


class BrowserPanel(ttk.Frame):
    """One tab's worth of UI: preview panel + file tree + status bar +
    copy/log buttons.

    Subclasses (DockerPanel, OpenShiftPanel) supply the navigation
    controls above the tree (container dropdown, or
    namespace/pod/container dropdowns) and implement `list_dir`,
    `copy_from_remote`, `copy_to_remote`, `describe_target`,
    `fetch_logs`, and `current_target_label`.
    """

    def __init__(self, master):
        super().__init__(master, padding=8)
        self._work_queue: "queue.Queue" = queue.Queue()
        self._path_of_item: dict[str, str] = {}
        self._build_preview()
        self._build_tree()
        self.after(100, self._poll_queue)

    # -- subclasses implement these -----------------------------------

    def list_dir(self, path: str):
        raise NotImplementedError

    def copy_from_remote(self, remote_path: str, local_path: str):
        raise NotImplementedError

    def copy_to_remote(self, local_path: str, remote_path: str):
        raise NotImplementedError

    def describe_target(self) -> TargetPreview:
        raise NotImplementedError

    def fetch_logs(self) -> str:
        raise NotImplementedError

    def current_target_label(self) -> str:
        raise NotImplementedError

    def ready(self) -> bool:
        """Whether enough of the nav controls are selected to browse."""
        raise NotImplementedError

    # -- activity log (shared widget, lives on the App/root window) -----

    def log_event(self, message: str):
        toplevel = self.winfo_toplevel()
        if hasattr(toplevel, "log_event"):
            toplevel.log_event(message)

    # -- preview panel ---------------------------------------------------

    def _build_preview(self):
        frame = ttk.LabelFrame(self, text="Preview")
        frame.pack(fill="x", pady=(0, 6))
        self.preview_text = tk.Text(
            frame, height=4, wrap="word", state="disabled",
            background="#f5f5f5", relief="flat", padx=6, pady=4,
        )
        self.preview_text.pack(fill="x")
        self._set_preview_text("Select a container/pod to see details here.")

    def _set_preview_text(self, text: str):
        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert("1.0", text)
        self.preview_text.configure(state="disabled")

    def update_preview(self):
        if not self.ready():
            return
        self._set_preview_text("Loading preview ...")

        def done(preview: TargetPreview):
            def apply():
                lines = [preview.title] + [f"  {k}: {v}" for k, v in preview.fields.items()]
                self._set_preview_text("\n".join(lines))

            self._work_queue.put(apply)

        def error(msg):
            self._work_queue.put(lambda: self._set_preview_text(f"(Preview unavailable: {msg})"))

        run_in_background(self.describe_target, done, error)

    # -- shared tree widget ---------------------------------------------

    def _build_tree(self):
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, pady=(0, 4))

        columns = ("size", "permissions", "modified")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings")
        self.tree.heading("#0", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("permissions", text="Permissions")
        self.tree.heading("modified", text="Modified")
        self.tree.column("#0", width=320)
        self.tree.column("size", width=80, anchor="e")
        self.tree.column("permissions", width=110)
        self.tree.column("modified", width=140)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.bind("<<TreeviewOpen>>", self._on_expand)

        # -- copy / logs buttons ---------------------------------------------
        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", pady=4)
        ttk.Button(btn_row, text="Copy to Local", command=self._copy_to_local).pack(side="left")
        ttk.Button(btn_row, text="Copy to Container", command=self._copy_to_remote).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(btn_row, text="View Logs", command=self._view_logs).pack(side="left", padx=(8, 0))
        ttk.Button(btn_row, text="Refresh", command=self.reload_root).pack(side="right")

        self.status_var = tk.StringVar(value="Select a container/pod to begin.")
        ttk.Label(self, textvariable=self.status_var, foreground="#444").pack(fill="x", pady=(4, 0))

    # -- queue plumbing so worker threads never touch Tk directly -------

    def _poll_queue(self):
        try:
            while True:
                fn = self._work_queue.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def set_status(self, text: str):
        self._work_queue.put(lambda: self.status_var.set(text))

    # -- tree population ---------------------------------------------

    def reload_root(self):
        if not self.ready():
            self.set_status("Select a container/pod to begin.")
            return
        self.log_event(f"Browsing root of {self.current_target_label()}")
        self.update_preview()
        self.tree.delete(*self.tree.get_children())
        self._path_of_item.clear()
        self._load_path_into("", "/")

    def _load_path_into(self, parent_item: str, path: str):
        self.set_status(f"Loading {path} ...")

        def done(entries):
            def apply():
                for entry in entries:
                    self._insert_entry(parent_item, path, entry)
                self.set_status(f"Loaded {path} ({len(entries)} entries) on {self.current_target_label()}")

            self._work_queue.put(apply)

        def error(msg):
            self._work_queue.put(lambda: (
                self.set_status(f"Error loading {path}"),
                self.log_event(f"ERROR listing {path}: {msg}"),
                messagebox.showerror("Error listing directory", msg),
            ))

        run_in_background(lambda: self.list_dir(path), done, error)

    def _insert_entry(self, parent_item: str, parent_path: str, entry: FileEntry):
        full_path = (parent_path.rstrip("/") + "/" + entry.name) if parent_path != "/" else "/" + entry.name
        item = self.tree.insert(
            parent_item,
            "end",
            text=entry.name,
            values=(entry.size, entry.permissions, entry.modified),
        )
        self._path_of_item[item] = full_path
        if entry.is_dir:
            # Insert a placeholder child so the expand arrow shows up;
            # real children are loaded lazily on <<TreeviewOpen>>.
            self.tree.insert(item, "end", text="", tags=(PLACEHOLDER,))

    def _on_expand(self, _event):
        item = self.tree.focus()
        children = self.tree.get_children(item)
        if len(children) == 1 and self.tree.tag_has(PLACEHOLDER, children[0]):
            self.tree.delete(children[0])
            self._load_path_into(item, self._path_of_item[item])

    # -- copy actions ---------------------------------------------------

    def _selected_remote_path(self) -> Optional[str]:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select a file or folder in the tree first.")
            return None
        return self._path_of_item.get(sel[0])

    def _copy_to_local(self):
        if not self.ready():
            messagebox.showinfo("Not ready", "Select a container/pod first.")
            return
        remote_path = self._selected_remote_path()
        if not remote_path:
            return
        dest_dir = filedialog.askdirectory(title="Choose local destination folder")
        if not dest_dir:
            return
        local_path = os.path.join(dest_dir, os.path.basename(remote_path))
        self.log_event(f"Copy to Local: {remote_path} -> {local_path}")
        self.set_status(f"Copying {remote_path} -> {local_path} ...")

        def done(_result):
            self._work_queue.put(lambda: (
                self.set_status(f"Copied to {local_path}"),
                self.log_event(f"Copy succeeded: {remote_path} -> {local_path}"),
                messagebox.showinfo("Copy complete", f"Copied:\n{remote_path}\n->\n{local_path}"),
            ))

        def error(msg):
            self._work_queue.put(lambda: (
                self.set_status("Copy failed"),
                self.log_event(f"ERROR copying {remote_path} -> {local_path}: {msg}"),
                messagebox.showerror("Copy failed", msg),
            ))

        run_in_background(lambda: self.copy_from_remote(remote_path, local_path), done, error)

    def _copy_to_remote(self):
        if not self.ready():
            messagebox.showinfo("Not ready", "Select a container/pod first.")
            return
        local_path = filedialog.askopenfilename(title="Choose local file to copy")
        if not local_path:
            return
        # Default destination folder = whatever directory is selected in
        # the tree, else '/'.
        selected = self.tree.selection()
        default_dir = "/"
        if selected:
            p = self._path_of_item.get(selected[0], "/")
            default_dir = p if p.endswith("/") or "." not in os.path.basename(p) else os.path.dirname(p) or "/"
        dest_path = simpledialog.askstring(
            "Destination path",
            "Full destination path inside the container/pod:",
            initialvalue=default_dir.rstrip("/") + "/" + os.path.basename(local_path),
        )
        if not dest_path:
            return
        self.log_event(f"Copy to Container: {local_path} -> {dest_path}")
        self.set_status(f"Copying {local_path} -> {dest_path} ...")

        def done(_result):
            self._work_queue.put(lambda: (
                self.set_status(f"Copied to {dest_path}"),
                self.log_event(f"Copy succeeded: {local_path} -> {dest_path}"),
                messagebox.showinfo("Copy complete", f"Copied:\n{local_path}\n->\n{dest_path}"),
                self.reload_root(),
            ))

        def error(msg):
            self._work_queue.put(lambda: (
                self.set_status("Copy failed"),
                self.log_event(f"ERROR copying {local_path} -> {dest_path}: {msg}"),
                messagebox.showerror("Copy failed", msg),
            ))

        run_in_background(lambda: self.copy_to_remote(local_path, dest_path), done, error)

    # -- logs viewer ---------------------------------------------------

    def _view_logs(self):
        if not self.ready():
            messagebox.showinfo("Not ready", "Select a container/pod first.")
            return

        win = tk.Toplevel(self)
        win.title(f"Logs - {self.current_target_label()}")
        win.geometry("760x480")

        text = tk.Text(win, wrap="word", state="disabled", font=("Courier New", 9))
        vsb = ttk.Scrollbar(win, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=vsb.set)
        text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vsb.pack(side="right", fill="y", pady=6)

        def set_text(content: str):
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", content)
            text.configure(state="disabled")

        def refresh():
            set_text("Loading logs ...")
            self.log_event(f"Viewing logs for {self.current_target_label()}")

            def done(content: str):
                win.after(0, lambda: set_text(content))

            def error(msg):
                win.after(0, lambda: (set_text(f"(Could not load logs)\n\n{msg}"), self.log_event(f"ERROR fetching logs: {msg}")))

            run_in_background(self.fetch_logs, done, error)

        def collect_to_file():
            # "Collect" = save the log output to a local file. If the
            # user has highlighted part of the text, save just that
            # selection; otherwise save everything currently shown.
            try:
                ranges = text.tag_ranges("sel")
                if ranges:
                    content = text.get(ranges[0], ranges[1])
                    scope = "selected lines"
                else:
                    content = text.get("1.0", "end-1c")
                    scope = "full log"
            except tk.TclError:
                content = text.get("1.0", "end-1c")
                scope = "full log"

            if not content.strip():
                messagebox.showinfo("Nothing to save", "There's no log content to save yet.")
                return

            safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in self.current_target_label())
            default_name = f"{safe_name}_{time.strftime('%Y%m%d_%H%M%S')}.log"
            dest_path = filedialog.asksaveasfilename(
                title="Save logs to file",
                initialfile=default_name,
                defaultextension=".log",
                filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")],
            )
            if not dest_path:
                return
            try:
                with open(dest_path, "w", encoding="utf-8") as fh:
                    fh.write(content)
            except OSError as exc:
                messagebox.showerror("Save failed", str(exc))
                self.log_event(f"ERROR saving logs to {dest_path}: {exc}")
                return

            self.log_event(f"Collected logs ({scope}) for {self.current_target_label()} -> {dest_path}")
            messagebox.showinfo("Logs saved", f"Saved {scope} to:\n{dest_path}")

        btn_row = ttk.Frame(win)
        ttk.Button(btn_row, text="Refresh", command=refresh).pack(side="left")
        ttk.Button(btn_row, text="Save to File...", command=collect_to_file).pack(side="left", padx=(8, 0))
        ttk.Label(btn_row, text="(select text first to save only that part)", foreground="#666").pack(
            side="left", padx=(10, 0)
        )
        btn_row.pack(side="bottom", fill="x", padx=6, pady=(0, 6))

        refresh()


class DockerPanel(BrowserPanel):
    """Task 4 & 5: Local Docker browsing + copy."""

    def __init__(self, master):
        self.backend = DockerBackend()
        self.container_var = tk.StringVar()

        # nav controls go above the preview/tree, so build them before
        # calling super().__init__ which packs those beneath.
        super().__init__(master)
        self._build_nav()
        self._nav_frame.pack(before=self.preview_text.master, fill="x")
        self.refresh_containers()

    def _build_nav(self):
        self._nav_frame = ttk.Frame(self)
        ttk.Label(self._nav_frame, text="Container:").pack(side="left")
        self.container_combo = ttk.Combobox(
            self._nav_frame, textvariable=self.container_var, state="readonly", width=30
        )
        self.container_combo.pack(side="left", padx=6)
        self.container_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_container_selected())
        ttk.Button(self._nav_frame, text="Refresh List", command=self.refresh_containers).pack(side="left")

    def refresh_containers(self):
        self.set_status("Listing running containers ...")

        def done(names):
            def apply():
                self.container_combo["values"] = names
                self.log_event(f"Found {len(names)} running container(s).")
                if names and not self.container_var.get():
                    self.container_var.set(names[0])
                    self.reload_root()
                self.set_status(
                    f"Found {len(names)} running container(s)."
                    + (" (using Docker SDK)" if self.backend.using_sdk else " (using docker CLI)")
                )

            self._work_queue.put(apply)

        def error(msg):
            self._work_queue.put(lambda: (
                self.set_status("Could not list containers"),
                self.log_event(f"ERROR listing containers: {msg}"),
                messagebox.showerror("Docker error", msg),
            ))

        run_in_background(self.backend.list_containers, done, error)

    def _on_container_selected(self):
        self.log_event(f"Container selected: {self.container_var.get()}")
        self.reload_root()

    def ready(self) -> bool:
        return bool(self.container_var.get())

    def current_target_label(self) -> str:
        return f"container '{self.container_var.get()}'"

    def list_dir(self, path: str):
        return self.backend.list_dir(self.container_var.get(), path)

    def describe_target(self) -> TargetPreview:
        return self.backend.describe_container(self.container_var.get())

    def fetch_logs(self) -> str:
        return self.backend.get_container_logs(self.container_var.get())

    def copy_from_remote(self, remote_path, local_path):
        return self.backend.copy_from_container(self.container_var.get(), remote_path, local_path)

    def copy_to_remote(self, local_path, remote_path):
        return self.backend.copy_to_container(self.container_var.get(), local_path, remote_path)


class OpenShiftPanel(BrowserPanel):
    """Task 7: Namespace -> Pod -> Container navigation + browsing/copy.

    Adds the Level-2 authentication flow described in the assignment:

        User -> API server + token -> oc login -> OpenShift
             -> authenticated session -> Projects / Pods / Logs

    Two ways to get that authenticated session, both available from the
    Login panel at the top of this tab:

    1. Enter an API server URL + token and click "Login with Token" -
       runs `oc login --token=... --server=...` to start a fresh session.
    2. Click "Use Existing Session" - checks whether `oc` already has an
       active, authenticated session (e.g. you ran `oc login` yourself
       in a terminal before launching the app) and reuses it as-is. This
       is also tried automatically, silently, when the tab first opens,
       so if you're already logged in you don't have to do anything.

    Either way, once authenticated, every other feature in this tab
    (namespace/pod/container browsing, file copy, logs) uses that same
    `oc` session - there's only ever one active session, matching the
    diagram's "authenticated session -> Projects / Pods / Logs" step.
    """

    def __init__(self, master):
        self.backend = OpenShiftBackend()
        self.namespace_var = tk.StringVar()
        self.pod_var = tk.StringVar()
        self.container_var = tk.StringVar()
        self.server_var = tk.StringVar()
        self.token_var = tk.StringVar()
        self.skip_tls_var = tk.BooleanVar(value=False)
        self.login_status_var = tk.StringVar(value="Not logged in.")
        self._authenticated = False
        super().__init__(master)
        self._build_login()
        self._build_nav()
        self._login_frame.pack(before=self.preview_text.master, fill="x")
        self._nav_frame.pack(before=self.preview_text.master, fill="x", pady=(6, 0))
        self._set_nav_enabled(False)
        # Try an already-authenticated oc session first, silently - if
        # the user ran `oc login` in a terminal before launching the
        # app, this picks it up with no action needed.
        self._use_existing_session(silent=True)

    # -- login panel ---------------------------------------------------

    def _build_login(self):
        self._login_frame = ttk.LabelFrame(self, text="OpenShift Login")

        row1 = ttk.Frame(self._login_frame)
        row1.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Label(row1, text="API Server:").pack(side="left")
        ttk.Entry(row1, textvariable=self.server_var, width=38).pack(side="left", padx=(4, 12))
        ttk.Label(row1, text="Token:").pack(side="left")
        ttk.Entry(row1, textvariable=self.token_var, width=28, show="*").pack(side="left", padx=(4, 0))

        row2 = ttk.Frame(self._login_frame)
        row2.pack(fill="x", padx=6, pady=(4, 4))
        ttk.Checkbutton(row2, text="Skip TLS verify (self-signed cert)", variable=self.skip_tls_var).pack(side="left")
        self.login_btn = ttk.Button(row2, text="Login with Token", command=self._login_with_token)
        self.login_btn.pack(side="left", padx=(12, 4))
        self.use_session_btn = ttk.Button(row2, text="Use Existing Session", command=lambda: self._use_existing_session(silent=False))
        self.use_session_btn.pack(side="left")
        self.logout_btn = ttk.Button(row2, text="Logout", command=self._logout, state="disabled")
        self.logout_btn.pack(side="left", padx=(4, 0))
        ttk.Label(row2, textvariable=self.login_status_var, foreground="#444").pack(side="left", padx=(12, 0))

    def _set_login_buttons_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.login_btn.configure(state=state)
        self.use_session_btn.configure(state=state)
        # Logout only ever makes sense once actually authenticated - even
        # when re-enabling the other two buttons, keep it disabled unless
        # we know there's a session to log out of.
        self.logout_btn.configure(state=("normal" if (enabled and self._authenticated) else "disabled"))

    def _set_nav_enabled(self, enabled: bool):
        state = "readonly" if enabled else "disabled"
        self.namespace_combo.configure(state=state)
        # Pod/container stay disabled until a namespace is actually picked,
        # same as before login gating existed.
        if not enabled:
            self.pod_combo.configure(state="disabled")
            self.container_combo.configure(state="disabled")

    def _on_authenticated(self, identity: str):
        self._authenticated = True
        server = self.backend.current_server()
        label = f"Logged in as: {identity}" + (f" ({server})" if server else "")
        self.login_status_var.set(label)
        self.log_event(f"OpenShift login succeeded: {label}")
        self.logout_btn.configure(state="normal")
        self._set_nav_enabled(True)
        self.refresh_namespaces()

    def _logout(self):
        server_label = self.backend.current_server() or "the current session"
        if not messagebox.askyesno(
            "Log out",
            f"Log out of {server_label}?\n\nThis runs 'oc logout', which ends the "
            f"session both here and on the server (the token can't be reused "
            f"elsewhere afterwards).",
        ):
            return

        self.login_status_var.set("Logging out ...")
        self._set_login_buttons_enabled(False)
        self.log_event(f"Logging out of {server_label} ...")

        def done(_result):
            self._work_queue.put(self._reset_after_logout)

        def error(msg):
            self._work_queue.put(lambda: (
                self._set_login_buttons_enabled(True),
                self.login_status_var.set("Logout failed."),
                self.log_event(f"ERROR: oc logout failed: {msg}"),
                messagebox.showerror("Logout failed", msg),
            ))

        run_in_background(self.backend.logout, done, error)

    def _reset_after_logout(self):
        """Clear all session/browsing state after a successful logout,
        so nothing from the old session (namespaces, pods, tree
        contents, preview) lingers on screen."""
        self._authenticated = False
        self._set_login_buttons_enabled(True)
        self.token_var.set("")
        self.login_status_var.set("Not logged in.")
        self.log_event("Logged out of OpenShift.")

        self.namespace_var.set("")
        self.pod_var.set("")
        self.container_var.set("")
        self.namespace_combo["values"] = []
        self.pod_combo["values"] = []
        self.container_combo["values"] = []
        self._set_nav_enabled(False)

        self.tree.delete(*self.tree.get_children())
        self._path_of_item.clear()
        self._set_preview_text("Select a container/pod to see details here.")
        self.set_status("Logged out. Log in again to continue.")

    def _login_with_token(self):
        server = self.server_var.get().strip()
        token = self.token_var.get().strip()
        if not server or not token:
            messagebox.showinfo("Missing info", "Enter both the API server URL and a token.")
            return
        self.login_status_var.set("Logging in ...")
        self._set_login_buttons_enabled(False)
        self.log_event(f"Attempting oc login to {server} (token hidden) ...")
        skip_tls = self.skip_tls_var.get()

        def done(identity):
            def apply():
                self._set_login_buttons_enabled(True)
                self._on_authenticated(identity)

            self._work_queue.put(apply)

        def error(msg):
            self._work_queue.put(lambda: (
                self._set_login_buttons_enabled(True),
                self.login_status_var.set("Login failed."),
                self.log_event(f"ERROR: oc login failed: {msg}"),
                messagebox.showerror("Login failed", msg),
            ))

        run_in_background(lambda: self.backend.login(server, token, skip_tls), done, error)

    def _use_existing_session(self, silent: bool):
        if not silent:
            self.login_status_var.set("Checking for an existing session ...")
            self._set_login_buttons_enabled(False)

        def done(identity):
            def apply():
                if not silent:
                    self._set_login_buttons_enabled(True)
                self._on_authenticated(identity)

            self._work_queue.put(apply)

        def error(msg):
            def apply():
                if not silent:
                    self._set_login_buttons_enabled(True)
                    messagebox.showinfo(
                        "No existing session",
                        "No authenticated oc session was found.\n\n"
                        "Either log in with a token above, or run 'oc login "
                        "<cluster_url> -u <user> -p <password>' in a terminal "
                        "first, then click 'Use Existing Session' again.",
                    )
                self.login_status_var.set("Not logged in.")
                self.log_event(
                    "No existing oc session found (this is expected on first launch)."
                    if silent else f"ERROR: {msg}"
                )

            self._work_queue.put(apply)

        run_in_background(self.backend.whoami, done, error)

    def _build_nav(self):
        self._nav_frame = ttk.Frame(self)

        row1 = ttk.Frame(self._nav_frame)
        row1.pack(fill="x")
        ttk.Label(row1, text="Namespace:").pack(side="left")
        self.namespace_combo = ttk.Combobox(row1, textvariable=self.namespace_var, state="readonly", width=28)
        self.namespace_combo.pack(side="left", padx=6)
        self.namespace_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_namespace_selected())
        ttk.Button(row1, text="Refresh", command=self.refresh_namespaces).pack(side="left")

        row2 = ttk.Frame(self._nav_frame)
        row2.pack(fill="x", pady=(4, 0))
        ttk.Label(row2, text="Pod:").pack(side="left")
        self.pod_combo = ttk.Combobox(row2, textvariable=self.pod_var, state="readonly", width=28)
        self.pod_combo.pack(side="left", padx=6)
        self.pod_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_pod_selected())

        ttk.Label(row2, text="Container:").pack(side="left", padx=(12, 0))
        self.container_combo = ttk.Combobox(row2, textvariable=self.container_var, state="readonly", width=20)
        self.container_combo.pack(side="left", padx=6)
        self.container_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_container_selected())

    # -- cascading dropdowns ---------------------------------------------

    def refresh_namespaces(self):
        self.set_status("Listing OpenShift projects (oc get projects) ...")

        def done(names):
            def apply():
                self.namespace_combo["values"] = names
                self.set_status(f"Found {len(names)} project(s).")
                self.log_event(f"Found {len(names)} OpenShift project(s).")

            self._work_queue.put(apply)

        def error(msg):
            self._work_queue.put(lambda: (
                self.set_status("Could not list projects"),
                self.log_event(f"ERROR listing projects: {msg}"),
                messagebox.showerror("OpenShift error", msg),
            ))

        run_in_background(self.backend.list_projects, done, error)

    def _on_namespace_selected(self):
        self.pod_var.set("")
        self.container_var.set("")
        self.pod_combo["values"] = []
        self.container_combo["values"] = []
        namespace = self.namespace_var.get()
        if not namespace:
            return
        self.log_event(f"Namespace selected: {namespace}")
        self.set_status(f"Listing pods in '{namespace}' ...")

        def done(pods):
            def apply():
                self.pod_combo["values"] = pods
                self.pod_combo.configure(state="readonly")
                self.set_status(f"Found {len(pods)} pod(s) in '{namespace}'.")
                self.log_event(f"Found {len(pods)} pod(s) in '{namespace}'.")

            self._work_queue.put(apply)

        def error(msg):
            self._work_queue.put(lambda: (
                self.set_status("Could not list pods"),
                self.log_event(f"ERROR listing pods in '{namespace}': {msg}"),
                messagebox.showerror("OpenShift error", msg),
            ))

        run_in_background(lambda: self.backend.list_pods(namespace), done, error)

    def _on_pod_selected(self):
        self.container_var.set("")
        self.container_combo["values"] = []
        namespace, pod = self.namespace_var.get(), self.pod_var.get()
        if not (namespace and pod):
            return
        self.log_event(f"Pod selected: {namespace}/{pod}")
        self.set_status(f"Inspecting containers in pod '{pod}' ...")

        def done(names):
            def apply():
                self.container_combo["values"] = names
                self.container_combo.configure(state="readonly")
                if names:
                    # Default to the first container, per spec.
                    self.container_var.set(names[0])
                    if len(names) > 1:
                        self.set_status(
                            f"Pod '{pod}' has {len(names)} containers: {', '.join(names)}. "
                            f"Defaulted to '{names[0]}'."
                        )
                        self.log_event(f"Pod '{pod}' has {len(names)} containers, defaulted to '{names[0]}'.")
                    self.reload_root()

            self._work_queue.put(apply)

        def error(msg):
            self._work_queue.put(lambda: (
                self.set_status("Could not inspect pod"),
                self.log_event(f"ERROR inspecting pod '{pod}': {msg}"),
                messagebox.showerror("OpenShift error", msg),
            ))

        run_in_background(lambda: self.backend.list_containers(namespace, pod), done, error)

    def _on_container_selected(self):
        self.log_event(f"Container selected: {self.container_var.get()}")
        self.reload_root()

    # -- BrowserPanel interface ---------------------------------------------

    def ready(self) -> bool:
        return bool(self.namespace_var.get() and self.pod_var.get() and self.container_var.get())

    def current_target_label(self) -> str:
        return f"{self.namespace_var.get()}/{self.pod_var.get()} (container: {self.container_var.get()})"

    def list_dir(self, path: str):
        return self.backend.list_dir(self.namespace_var.get(), self.pod_var.get(), self.container_var.get(), path)

    def describe_target(self) -> TargetPreview:
        return self.backend.describe_pod(self.namespace_var.get(), self.pod_var.get(), self.container_var.get())

    def fetch_logs(self) -> str:
        return self.backend.get_pod_logs(self.namespace_var.get(), self.pod_var.get(), self.container_var.get())

    def copy_from_remote(self, remote_path, local_path):
        return self.backend.copy_from_pod(
            self.namespace_var.get(), self.pod_var.get(), self.container_var.get(), remote_path, local_path
        )

    def copy_to_remote(self, local_path, remote_path):
        return self.backend.copy_to_pod(
            self.namespace_var.get(), self.pod_var.get(), self.container_var.get(), local_path, remote_path
        )


class App(tk.Tk):
    """Root window: the two browsing tabs, plus a shared Activity Log
    panel docked at the bottom that both tabs write to via
    `BrowserPanel.log_event()`. This is the only place activity/errors
    are surfaced - there is no log file and nothing goes to the
    terminal, everything is visible right here in the UI."""

    def __init__(self):
        super().__init__()
        self.title(f"Container File System Explorer  v{APP_VERSION}")
        self.geometry("940x760")

        # Catch any exception raised inside a Tk callback (button click,
        # combobox selection, etc) and surface it in the Activity Log
        # instead of letting it vanish into stderr.
        self.report_callback_exception = self._handle_tk_exception

        self._build_menu()

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        docker_tab = DockerPanel(notebook)
        openshift_tab = OpenShiftPanel(notebook)

        notebook.add(docker_tab, text="Local Docker")
        notebook.add(openshift_tab, text="OpenShift")

        self._build_activity_log()
        self.log_event(f"Container File System Explorer v{APP_VERSION} started.")

        # Run the first-run wizard (if needed) after the main window has
        # laid itself out, so it can center itself over a real window -
        # and check for updates a moment after that, off the critical
        # path of startup.
        self.after(50, self._maybe_show_setup_wizard)
        self.after(1200, self._check_for_updates)

    # -- first-run wizard ---------------------------------------------

    def _maybe_show_setup_wizard(self):
        if is_first_run():
            self.log_event("First launch detected - showing setup wizard.")
            SetupWizard(self).show()
            self.log_event("Setup wizard completed.")

    def _run_setup_wizard_again(self):
        SetupWizard(self).show()

    # -- update checker ---------------------------------------------

    def _check_for_updates(self):
        def worker():
            return check_for_update(timeout=3)

        def done(latest_version):
            if latest_version:
                self.log_event(f"Update available: v{latest_version} (you have v{APP_VERSION}).")
                self._prompt_update(latest_version)
            # else: already up to date, or the check failed/timed out
            # (e.g. offline) - either way, silent. Not being able to
            # check for updates shouldn't bother the user.

        def error(_msg):
            pass  # same reasoning - fail silently, this is a courtesy check

        run_in_background(worker, done, error)

    def _prompt_update(self, latest_version: str):
        if messagebox.askyesno(
            "Update available",
            f"A newer version is available: v{latest_version}\n"
            f"You're currently running: v{APP_VERSION}\n\n"
            f"Open the download page now?",
        ):
            webbrowser.open(RELEASES_PAGE_URL)

    def _check_for_updates_now(self):
        """Manual 'Check for Updates' menu item - same as the silent
        startup check, but always tells the user the result either way,
        since this time they explicitly asked."""
        self.log_event("Checking for updates ...")

        def worker():
            return check_for_update(timeout=5)

        def done(latest_version):
            if latest_version:
                self._prompt_update(latest_version)
            else:
                self.log_event(f"You're up to date (v{APP_VERSION}).")
                messagebox.showinfo("Up to date", f"You're running the latest version (v{APP_VERSION}).")

        def error(_msg):
            messagebox.showwarning(
                "Could not check for updates",
                "Couldn't reach GitHub to check for updates. Check your internet connection and try again.",
            )

        run_in_background(worker, done, error)

    # -- menu ---------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Run Setup Wizard Again", command=self._run_setup_wizard_again)
        help_menu.add_command(label="Check for Updates Now", command=self._check_for_updates_now)
        help_menu.add_separator()
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.configure(menu=menubar)

    def _show_about(self):
        messagebox.showinfo(
            "About",
            f"Container File System Explorer\nVersion {APP_VERSION}\n\n"
            f"Browse and copy files between your local machine and Docker "
            f"containers / OpenShift pods.",
        )

    # -- activity log panel ---------------------------------------------

    def _build_activity_log(self):
        frame = ttk.LabelFrame(self, text="Activity Log")
        frame.pack(fill="both", padx=8, pady=(0, 8))

        self.activity_text = tk.Text(
            frame, height=7, wrap="word", state="disabled", font=("Courier New", 9)
        )
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.activity_text.yview)
        self.activity_text.configure(yscrollcommand=vsb.set)
        self.activity_text.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        ttk.Button(frame, text="Clear", command=self._clear_activity_log).pack(side="bottom", anchor="e", pady=2, padx=2)

    def log_event(self, message: str):
        """Append a timestamped line to the Activity Log panel. Safe to
        call from any Tk callback running on the main thread (background
        threads should go through a panel's work-queue first, same as
        every other UI update in this app)."""
        timestamp = time.strftime("%H:%M:%S")
        self.activity_text.configure(state="normal")
        self.activity_text.insert("end", f"[{timestamp}] {message}\n")
        self.activity_text.see("end")
        self.activity_text.configure(state="disabled")

    def _clear_activity_log(self):
        self.activity_text.configure(state="normal")
        self.activity_text.delete("1.0", "end")
        self.activity_text.configure(state="disabled")

    def _handle_tk_exception(self, exc_type, exc_value, exc_traceback):
        self.log_event(f"ERROR (unexpected): {exc_type.__name__}: {exc_value}")


if __name__ == "__main__":
    App().mainloop()
