# Container File System Explorer

A small toolkit for browsing and moving files in and out of Docker
containers and OpenShift/Kubernetes pods — built for the Technical
Trainee Assignment (Container File System Explorer).

It has two entry points:

| File               | Assignment tasks | What it is                                                   |
|--------------------|-------------------|---------------------------------------------------------------|
| `cli_explorer.py`  | Task 3            | Command-line tool: list a container's file system as a tree  |
| `gui_explorer.py`  | Tasks 4, 5, 7     | Desktop GUI: browse + copy files, Docker **and** OpenShift    |
| `backends.py`      | —                 | Shared library both tools import (Docker/OpenShift + parsing)|

## 1. Install

```bash
# 1. Clone / unzip this folder, then from inside it:
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

**Prerequisites on your machine (not installed by pip):**

- **Docker CLI** — required for Task 1/2 and for `docker exec` /
  `docker cp` fallback. Install from
  https://docs.docker.com/engine/install/. Docker Desktop is *not*
  required, the CLI/engine is enough.
- **`oc` CLI** — required for Level 2 (Task 6/7). Download it from your
  OpenShift console's *Command Line Tools* menu. You don't need to log
  in beforehand — the app's OpenShift tab has its own login panel (see
  below) — but you can if you'd rather:
  ```bash
  oc login <cluster_url> -u <username> -p <password>
  ```
- **tkinter** — ships with Python on Windows/macOS. On Linux it's a
  separate OS package:
  ```bash
  sudo apt install python3-tk      # Ubuntu/Debian
  sudo dnf install python3-tkinter # Fedora
  ```

## 2. Run the CLI tool (Task 3)

```bash
python cli_explorer.py --container my_container --path /etc
python cli_explorer.py -c my_container -p /etc --long   # detailed columns
```

Example output:

```
/etc
├── hosts
├── hostname
├── passwd
└── resolv.conf
```

Errors (container not running, bad path, Docker not installed) are
caught and printed as a one-line message rather than a stack trace.

## 3. Run the GUI tool (Tasks 4, 5, 7)

```bash
python gui_explorer.py
```

This opens a window with two tabs, each laid out top to bottom as
**navigation controls → Preview panel → file tree → buttons**, with a
shared **Activity Log** docked at the bottom of the whole window:

- **Local Docker** — pick a running container from the dropdown. The
  **Preview** panel above the tree fills in with the container's image,
  status, start time, and published ports (`docker inspect`). The tree
  loads the root directory; click the arrow next to any folder to
  lazily expand it (children are only fetched from Docker when you open
  a folder, not the whole tree up front). Select a file/folder and use
  **Copy to Local** or **Copy to Container** to transfer files in either
  direction, or **View Logs** to open the container's stdout/stderr log
  (`docker logs`) in its own scrollable window with **Refresh** and
  **Save to File...** buttons — the latter collects the log output to a
  local `.log` file; highlight part of the text first to save just that
  selection, or leave nothing selected to save everything shown.
- **OpenShift** — the tab opens with an **OpenShift Login** panel at
  the top. Two ways to authenticate:
  1. Enter your cluster's **API Server** URL and a **Token**, tick
     **Skip TLS verify** if the cluster uses a self-signed certificate,
     and click **Login with Token** (runs `oc login --token=... --server=...`).
     The token field is masked and is never shown in the Activity Log or
     any error message.
  2. Click **Use Existing Session** if you already ran `oc login`
     yourself in a terminal — the app just checks `oc whoami` and reuses
     that session. This is also tried automatically and silently the
     moment the tab opens, so if you're already logged in there's
     nothing to click.

  Once authenticated, the status line shows **"Logged in as: `<user>`
  (`<server>`)"**, a **Logout** button becomes active, and the
  **Namespace → Pod → Container** dropdowns unlock (populated live from
  `oc get projects` / `oc get pods` / `oc get pod ... -o jsonpath`). If
  a pod has multiple containers, the dropdown lists all of them and
  defaults to the first, as required. Clicking **Logout** (after a
  confirmation prompt) runs `oc logout` — this ends the session both
  locally and on the server, so the token can't be reused elsewhere
  afterwards — clears the token field, and resets the tab back to its
  logged-out state (namespace/pod/container selections, tree, and
  preview all cleared).

  From here on everything uses that one `oc` session: the **Preview**
  panel (pod phase, node, selected container's ready/restart-count/
  image), the file tree, **Copy to Local**/**Copy to Container**
  (`oc cp`, with the same Windows-path and missing-`tar` fallbacks as
  Docker), and **View Logs** (`oc logs --tail=2000`, with the same Save
  to File collection) — all backed by `oc exec`/`oc cp`/`oc logs`
  against whichever session is currently active. Docker's **View Logs**
  fetches the same 2000-line window via `docker logs --tail 2000`.
- **Activity Log** (bottom of the window) — a running, timestamped
  record of everything the app has done in this session: logins,
  selections, directory listings, copy attempts (success or failure,
  with the error), log views, and log collection (where the file was
  saved). This is the only place activity is recorded — there's no log
  file and nothing is printed to a terminal, so this panel is what to
  screenshot when reporting a bug. It has a **Clear** button to reset it.

All `docker`/`oc` calls run on a background thread, so the window stays
responsive while a listing, preview, log fetch, or copy is in progress
(the status line under each tree, and the Activity Log, show what's
happening).

## 4. Project layout

```
ContainerExplorer/
├── backends.py           # DockerBackend, OpenShiftBackend, ls -la parser
├── cli_explorer.py        # Task 3
├── gui_explorer.py        # Tasks 4, 5, 7 (Preview panel, Activity Log, Logs viewer)
├── version.py             # Single source of truth for the version number
├── first_run.py           # First-launch setup wizard (checks docker/oc are installed)
├── update_checker.py      # Checks GitHub Releases for a newer version
├── installer.iss          # Inno Setup script -> one-click Windows installer
├── .github/workflows/
│   └── release.yml        # Auto-builds + publishes the installer on every version tag
├── BUILD.md                # How to package and release this as an installer
├── requirements.txt
└── README.md
```

`backends.py` is shared so the "how do I talk to Docker/OpenShift"
logic is written once and unit-testable independent of any UI. Both
`DockerBackend` and `OpenShiftBackend` raise a single `BackendError`
for any expected failure (container not running, path not found, no
permission, `oc`/`docker` not installed, not logged in, etc.), which
both `cli_explorer.py` and `gui_explorer.py` catch and turn into a
friendly message. The Activity Log panel in the GUI is the only place
these errors and other actions are surfaced — there's no log file and
nothing is written to a terminal.

**Distributing this to non-technical end users?** See `BUILD.md` — it
covers packaging `gui_explorer.py` into a single-click Windows
installer (no Python/pip required) and setting up automatic builds so
future code changes reach users without you manually re-sending a zip
file each time. The app also has a **Help** menu with "Check for
Updates Now" and "Run Setup Wizard Again".

## 5. AI assistance disclosure

This codebase (backends.py, cli_explorer.py, gui_explorer.py, this
README, and the accompanying write-up) was generated with the help of
Claude (Anthropic), as explicitly permitted by the assignment brief.
Claude wrote the initial implementation of all three files in one pass
from the assignment's requirements; it was then reviewed, compiled
(`python -m py_compile`), and the `ls -la` parser in `backends.py` was
unit-tested against sample output (including edge cases like symlinks
and filenames containing spaces) to check correctness before being
treated as final.
