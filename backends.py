"""
backends.py
-----------
Shared backend logic for talking to Docker containers and OpenShift
(Kubernetes) pods/containers.

Both the CLI tool (cli_explorer.py) and the GUI tool (gui_explorer.py)
import from this module so the "how do I talk to docker / oc" logic only
lives in one place.

Design notes
~~~~~~~~~~~~
- DockerBackend prefers the official Docker SDK for Python
  (`docker.from_env()`), because it gives structured data (container
  objects, exec_run with exit codes) instead of scraping text. If the SDK
  or the docker daemon isn't available, it transparently falls back to
  shelling out to the `docker` CLI with subprocess, which is what the
  assignment's "Hints" section describes.
- OpenShiftBackend uses `subprocess` to call the `oc` CLI, as recommended
  in the assignment ("subprocess calling oc is faster to get working").
- Both backends expose the same shape of data (FileEntry) so the GUI can
  render Docker and OpenShift file trees with identical code.
- Activity/error reporting is done entirely through the GUI's own
  Activity Log panel (see gui_explorer.py) rather than a file or console
  logger - this module stays silent and only communicates via return
  values and BackendError.
"""

from __future__ import annotations

import json
import os
import subprocess
import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# --------------------------------------------------------------------------
# Shared data model
# --------------------------------------------------------------------------

@dataclass
class FileEntry:
    """A single file/dir entry as returned by `ls -la`."""
    name: str
    is_dir: bool
    permissions: str
    size: str
    modified: str
    raw: str = ""  # original line, kept for debugging


@dataclass
class TargetPreview:
    """Summary info shown in the GUI's 'Preview' panel for whatever
    container/pod is currently selected. Fields are display-ready
    strings (already formatted / defaulted) so the GUI can render them
    directly without needing to know the source shape."""
    title: str
    fields: Dict[str, str] = field(default_factory=dict)


class BackendError(Exception):
    """Raised for any expected, user-facing failure (not running, no
    permission, path not found, etc). The GUI/CLI catch this and show a
    friendly message instead of a raw traceback."""


# --------------------------------------------------------------------------
# ls -la parsing (shared by both backends since both ultimately run
# `ls -la <path>` inside the target container)
# --------------------------------------------------------------------------

def parse_ls_la(output: str) -> List[FileEntry]:
    """Parse the output of `ls -la` into a list of FileEntry.

    Handles the standard unix long-listing format:
        drwxr-xr-x  2 root root 4096 Jan 10 12:34 name
    Skips the leading 'total N' line and '.' / '..' entries.
    """
    entries: List[FileEntry] = []
    for line in output.splitlines():
        line = line.rstrip()
        if not line or line.startswith("total "):
            continue
        parts = line.split(maxsplit=8)
        if len(parts) < 9:
            # Line we don't understand (e.g. an error message that leaked
            # into stdout) - skip rather than crash the whole listing.
            continue
        perms, _links, _owner, _group, size, month, day, time_or_year, name = parts
        if name in (".", ".."):
            continue
        # Handle "name -> target" for symlinks
        display_name = name.split(" -> ")[0]
        entries.append(
            FileEntry(
                name=display_name,
                is_dir=perms.startswith("d"),
                permissions=perms,
                size=size,
                modified=f"{month} {day} {time_or_year}",
                raw=line,
            )
        )
    # Directories first, then alphabetical - nicer for a tree view.
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


def _run(cmd: List[str], timeout: int = 30, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
    except FileNotFoundError as exc:
        raise BackendError(
            f"Command not found: '{cmd[0]}'. Is it installed and on your PATH?"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"Command timed out: {' '.join(cmd)}") from exc


def _run_binary(
    cmd: List[str], timeout: int = 60, input_bytes: Optional[bytes] = None
) -> subprocess.CompletedProcess:
    """Like _run, but works in bytes rather than text - used for streaming
    a file's raw content through `... cat`/`... sh -c "cat > x"` fallbacks,
    where decoding as UTF-8 text would corrupt binary files."""
    try:
        return subprocess.run(
            cmd, capture_output=True, timeout=timeout, input=input_bytes
        )
    except FileNotFoundError as exc:
        raise BackendError(
            f"Command not found: '{cmd[0]}'. Is it installed and on your PATH?"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"Command timed out: {' '.join(cmd)}") from exc


# `oc cp` / `docker cp` copy directories by running `tar` *inside* the
# target container and streaming/unpacking the archive. Minimal or
# distroless images often don't ship `tar`, so that step fails with a
# generic wrapped exit code (commonly 255) that doesn't say "tar" anywhere.
# Both backends fall back to a tar-free, single-file transfer via
# `exec ... cat` when the primary `cp` fails, and mention this hint in the
# final error if the fallback fails too (e.g. because the target really is
# a directory, which `cat` can't stream either).
_TAR_MISSING_HINT = (
    "This can happen when the container image doesn't have 'tar' "
    "installed (common in minimal/distroless images) - 'cp' relies on "
    "tar internally, even for a single file. If the item you selected is "
    "a directory, copying whole directories isn't possible without tar "
    "in the container; try copying individual files inside it instead."
)


def _split_local_path_for_cp(local_path: str):
    """`oc cp` / `docker cp` decide which of their two arguments is the
    "remote" one by looking for a colon (expected format
    'namespace/pod:path' or 'container:path'). On Windows, an absolute
    local path like 'C:/Users/me/file.txt' *also* contains a colon right
    after the drive letter, so the CLI can end up seeing a colon in both
    arguments and refusing with:

        error: one of src or dest must be a local file specification

    The fix used throughout this module: never pass an absolute,
    drive-lettered local path directly. Instead, split it into a
    directory (used as the subprocess `cwd`) and a bare filename (which
    has no colon at all and is unambiguous).

    Returns (cwd, relative_filename).
    """
    directory = os.path.dirname(os.path.abspath(local_path)) or "."
    filename = os.path.basename(local_path)
    return directory, filename


# --------------------------------------------------------------------------
# Docker backend (Level 1, Task 3-5)
# --------------------------------------------------------------------------

class DockerBackend:
    """Talks to the local Docker engine.

    Tries the Docker SDK first (`import docker`), falls back to the
    `docker` CLI via subprocess if the SDK isn't installed. Either way the
    public methods below return the same plain-Python data structures.
    """

    def __init__(self):
        self._client = None
        try:
            import docker  # type: ignore
            self._client = docker.from_env()
            self._client.ping()
        except Exception:
            # SDK not installed, daemon not reachable, permission denied,
            # etc. We'll fall back to the CLI for every call below.
            self._client = None

    @property
    def using_sdk(self) -> bool:
        return self._client is not None

    # -- discovery ---------------------------------------------------

    def list_containers(self, all_containers: bool = False) -> List[str]:
        """Return names of running containers (or all, if requested)."""
        if self._client is not None:
            try:
                containers = self._client.containers.list(all=all_containers)
                return [c.name for c in containers]
            except Exception as exc:
                raise BackendError(f"Docker SDK error listing containers: {exc}")

        if shutil.which("docker") is None:
            raise BackendError(
                "Docker CLI not found on PATH. Install Docker "
                "(see docs.docker.com/engine/install) and try again."
            )
        cmd = ["docker", "ps", "--format", "{{.Names}}"]
        if all_containers:
            cmd = ["docker", "ps", "-a", "--format", "{{.Names}}"]
        result = _run(cmd)
        if result.returncode != 0:
            raise BackendError(result.stderr.strip() or "docker ps failed")
        return [line for line in result.stdout.splitlines() if line]

    # -- preview / describe ---------------------------------------------

    def describe_container(self, container: str) -> TargetPreview:
        """Summary info for the Preview panel: image, status, created
        time, and published ports. Uses `docker inspect` (JSON) so it
        works whether or not the SDK is installed."""
        result = _run(["docker", "inspect", container])
        if result.returncode != 0:
            raise BackendError(result.stderr.strip() or f"Could not inspect container '{container}'.")
        try:
            data = json.loads(result.stdout)[0]
        except (json.JSONDecodeError, IndexError, KeyError) as exc:
            raise BackendError(f"Could not parse 'docker inspect' output: {exc}")

        state = data.get("State", {})
        config = data.get("Config", {})
        ports = data.get("NetworkSettings", {}).get("Ports", {}) or {}
        port_list = ", ".join(sorted(ports.keys())) or "(none published)"

        return TargetPreview(
            title=f"Container: {container}",
            fields={
                "Image": config.get("Image", "unknown"),
                "Status": state.get("Status", "unknown"),
                "Started": state.get("StartedAt", "")[:19].replace("T", " ") or "n/a",
                "Ports": port_list,
                "Container ID": data.get("Id", "")[:12],
            },
        )

    # -- logs ---------------------------------------------------

    def get_container_logs(self, container: str, tail: int = 2000) -> str:
        """Fetch the last `tail` lines of the container's stdout/stderr
        log, for the GUI's 'View Logs' panel. Equivalent to
        `docker logs --tail N <container>`."""
        result = _run(["docker", "logs", "--tail", str(tail), container], timeout=45)
        if result.returncode != 0:
            raise BackendError(result.stderr.strip() or f"Could not fetch logs for '{container}'.")
        # docker logs writes to both stdout and stderr depending on the
        # stream the process wrote to; show both, stdout first.
        combined = result.stdout
        if result.stderr.strip():
            combined += ("\n" if combined else "") + result.stderr
        return combined or "(no log output)"

    # -- file system ---------------------------------------------------

    def list_dir(self, container: str, path: str = "/") -> List[FileEntry]:
        if self._client is not None:
            try:
                c = self._client.containers.get(container)
                if c.status != "running":
                    raise BackendError(
                        f"Container '{container}' is not running (status: {c.status})."
                    )
                exit_code, output = c.exec_run(["ls", "-la", path], demux=False)
                text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
                if exit_code != 0:
                    raise BackendError(
                        f"Path not found or inaccessible inside container: {path}\n{text.strip()}"
                    )
                return parse_ls_la(text)
            except BackendError:
                raise
            except Exception as exc:
                # e.g. docker.errors.NotFound
                raise BackendError(f"Could not inspect container '{container}': {exc}")

        result = _run(["docker", "exec", container, "ls", "-la", path])
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "No such container" in stderr:
                raise BackendError(f"No such container: '{container}'.")
            if "is not running" in stderr:
                raise BackendError(f"Container '{container}' is not running.")
            raise BackendError(stderr or f"Could not list '{path}' in '{container}'.")
        return parse_ls_la(result.stdout)

    # -- file copy ---------------------------------------------------

    def copy_from_container(self, container: str, container_path: str, local_path: str) -> None:
        # Run with cwd = destination folder and pass only the bare
        # filename as the local argument, so an absolute Windows path
        # (which has its own drive-letter colon) never confuses docker's
        # "container:path" parsing. See _split_local_path_for_cp.
        cwd, filename = _split_local_path_for_cp(local_path)
        result = _run(["docker", "cp", f"{container}:{container_path}", filename], timeout=120, cwd=cwd)
        if result.returncode == 0:
            return

        cp_error = result.stderr.strip() or f"docker cp failed with exit code {result.returncode}."

        # Fallback: stream the file out via `docker exec ... cat`, which
        # doesn't need tar inside the container at all. Only meaningful
        # for a single file - if container_path is a directory this will
        # fail too, with a clear "Is a directory" message.
        fallback = _run_binary(["docker", "exec", container, "cat", container_path])
        if fallback.returncode == 0:
            with open(local_path, "wb") as fh:
                fh.write(fallback.stdout)
            return

        fallback_error = fallback.stderr.decode("utf-8", errors="replace").strip()
        raise BackendError(
            f"docker cp failed: {cp_error}\n"
            f"Fallback copy also failed: {fallback_error or 'unknown error'}\n\n"
            f"{_TAR_MISSING_HINT}"
        )

    def copy_to_container(self, container: str, local_path: str, container_path: str) -> None:
        cwd, filename = _split_local_path_for_cp(local_path)
        result = _run(["docker", "cp", filename, f"{container}:{container_path}"], timeout=120, cwd=cwd)
        if result.returncode == 0:
            return

        cp_error = result.stderr.strip() or f"docker cp failed with exit code {result.returncode}."

        # Fallback: stream the local file's bytes in via `docker exec -i
        # ... sh -c "cat > path"`, which doesn't need tar inside the
        # container either.
        with open(local_path, "rb") as fh:
            data = fh.read()
        fallback = _run_binary(
            ["docker", "exec", "-i", container, "sh", "-c", f"cat > '{container_path}'"],
            input_bytes=data,
        )
        if fallback.returncode == 0:
            return

        fallback_error = fallback.stderr.decode("utf-8", errors="replace").strip()
        raise BackendError(
            f"docker cp failed: {cp_error}\n"
            f"Fallback copy also failed: {fallback_error or 'unknown error'}\n\n"
            f"{_TAR_MISSING_HINT}"
        )


# --------------------------------------------------------------------------
# OpenShift backend (Level 2, Task 6-7)
# --------------------------------------------------------------------------

class OpenShiftBackend:
    """Talks to an OpenShift/Kubernetes cluster via the `oc` CLI.

    Two ways to get an authenticated session, matching the assignment's
    Level 2 auth flow (User -> API server + token -> `oc login` ->
    OpenShift -> authenticated session -> Projects/Pods/Logs):

    1. `login(server, token)` - runs `oc login --token=... --server=...`
       to establish a brand new session from credentials entered in the
       GUI.
    2. `whoami()` - checks whether an `oc` session is *already* active
       (e.g. the user ran `oc login` themselves in a terminal before
       launching the app) and, if so, reuses it as-is.

    Either way, every method below this point (list_projects, list_pods,
    list_dir, copy_*, get_pod_logs, ...) just uses whatever session is
    currently active in `oc`'s config - this class doesn't track or
    store credentials itself, it relies entirely on `oc`'s own session
    state on disk (~/.kube/config), same as the `oc` CLI would if you
    ran it by hand.
    """

    def _check_oc(self):
        if shutil.which("oc") is None:
            raise BackendError(
                "The 'oc' CLI was not found on PATH. Install it from the "
                "OpenShift console (Command Line Tools) and log in with "
                "'oc login <cluster_url> -u <user> -p <password>'."
            )

    def login(self, server: str, token: str, insecure_skip_tls_verify: bool = False) -> str:
        """Log in with an API server URL + token (`oc login --token=...
        --server=...`), establishing a new authenticated session. Returns
        the identity string on success (same as whoami()); raises
        BackendError with oc's own message on failure (bad token,
        unreachable server, expired token, TLS certificate problems).

        The token itself is never included in any exception message or
        surfaced back to the caller - only oc's stderr is, and oc does
        not echo the token back on failure.
        """
        self._check_oc()
        server = server.strip()
        token = token.strip()
        if not server:
            raise BackendError("API server URL is required (e.g. https://api.cluster.example.com:6443).")
        if not token:
            raise BackendError("Token is required.")

        cmd = ["oc", "login", f"--token={token}", f"--server={server}"]
        if insecure_skip_tls_verify:
            cmd.append("--insecure-skip-tls-verify=true")

        result = _run(cmd, timeout=30)
        if result.returncode != 0:
            stderr = result.stderr.strip() or "oc login failed."
            if "x509" in stderr or "certificate" in stderr.lower():
                stderr += "\n\nIf this cluster uses a self-signed certificate, try the 'Skip TLS verify' option."
            raise BackendError(stderr)

        return self.whoami()

    def whoami(self) -> str:
        self._check_oc()
        result = _run(["oc", "whoami"])
        if result.returncode != 0:
            raise BackendError(
                "Not logged in to any OpenShift cluster. Log in with a "
                "token above, or run 'oc login <cluster_url> -u <user> "
                "-p <password>' in a terminal first."
            )
        return result.stdout.strip()

    def current_server(self) -> str:
        """Best-effort: the API server URL of whatever session is active,
        for display next to the logged-in identity. Returns '' if this
        can't be determined (shouldn't block anything if it fails)."""
        try:
            result = _run(["oc", "whoami", "--show-server"], timeout=10)
            return result.stdout.strip() if result.returncode == 0 else ""
        except BackendError:
            return ""

    def logout(self) -> str:
        """End the current oc session (`oc logout`). This both clears
        the locally-cached credentials in ~/.kube/config *and* asks the
        OpenShift API server to invalidate the token server-side, so the
        same token can't be reused elsewhere after logging out here."""
        self._check_oc()
        result = _run(["oc", "logout"], timeout=15)
        if result.returncode != 0:
            raise BackendError(result.stderr.strip() or "oc logout failed.")
        return result.stdout.strip() or "Logged out."

    def list_projects(self) -> List[str]:
        self._check_oc()
        result = _run(["oc", "get", "projects", "-o", "jsonpath={.items[*].metadata.name}"])
        if result.returncode != 0:
            raise BackendError(result.stderr.strip() or "Could not list projects.")
        return result.stdout.split()

    def list_pods(self, namespace: str) -> List[str]:
        self._check_oc()
        result = _run(["oc", "get", "pods", "-n", namespace, "-o", "jsonpath={.items[*].metadata.name}"])
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "Forbidden" in stderr:
                raise BackendError(f"No permission to list pods in namespace '{namespace}'.")
            raise BackendError(stderr or f"Could not list pods in '{namespace}'.")
        return result.stdout.split()

    def get_pod_status(self, namespace: str, pod: str) -> str:
        self._check_oc()
        result = _run(["oc", "get", "pod", pod, "-n", namespace, "-o", "jsonpath={.status.phase}"])
        if result.returncode != 0:
            raise BackendError(result.stderr.strip() or f"Could not get status for pod '{pod}'.")
        return result.stdout.strip()

    def list_containers(self, namespace: str, pod: str) -> List[str]:
        self._check_oc()
        result = _run(
            ["oc", "get", "pod", pod, "-n", namespace, "-o", "jsonpath={.spec.containers[*].name}"]
        )
        if result.returncode != 0:
            raise BackendError(result.stderr.strip() or f"Could not list containers in pod '{pod}'.")
        names = result.stdout.split()
        if not names:
            raise BackendError(f"Pod '{pod}' reported no containers.")
        return names

    # -- preview / describe ---------------------------------------------

    def describe_pod(self, namespace: str, pod: str, container: str) -> TargetPreview:
        """Summary info for the Preview panel: phase, node, start time,
        and per-container ready/restart-count/image, pulled from a
        single `oc get pod -o json` call."""
        self._check_oc()
        result = _run(["oc", "get", "pod", pod, "-n", namespace, "-o", "json"])
        if result.returncode != 0:
            raise BackendError(result.stderr.strip() or f"Could not describe pod '{pod}'.")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BackendError(f"Could not parse 'oc get pod -o json' output: {exc}")

        status = data.get("status", {})
        spec = data.get("spec", {})
        fields = {
            "Namespace": namespace,
            "Phase": status.get("phase", "unknown"),
            "Node": spec.get("nodeName", "n/a"),
            "Started": (status.get("startTime") or "n/a").replace("T", " ").rstrip("Z"),
        }

        container_statuses = {cs.get("name"): cs for cs in status.get("containerStatuses", [])}
        cs = container_statuses.get(container)
        if cs:
            fields["Selected container"] = container
            fields["Container ready"] = str(cs.get("ready", False))
            fields["Restart count"] = str(cs.get("restartCount", 0))
            image = cs.get("image", "unknown")
            fields["Image"] = image
        total_containers = len(spec.get("containers", []))
        if total_containers > 1:
            fields["Containers in pod"] = f"{total_containers} ({', '.join(container_statuses.keys())})"

        return TargetPreview(title=f"Pod: {pod}", fields=fields)

    # -- logs ---------------------------------------------------

    def get_pod_logs(self, namespace: str, pod: str, container: str, tail: int = 2000) -> str:
        """Fetch the last `tail` lines of the selected container's log
        inside the pod, for the GUI's 'View Logs' panel. Equivalent to
        `oc logs <pod> -c <container> -n <namespace> --tail=N`."""
        self._check_oc()
        result = _run(
            ["oc", "logs", pod, "-c", container, "-n", namespace, f"--tail={tail}"],
            timeout=45,
        )
        if result.returncode != 0:
            raise BackendError(result.stderr.strip() or f"Could not fetch logs for '{pod}/{container}'.")
        return result.stdout or "(no log output)"

    # -- file system ---------------------------------------------------

    def list_dir(self, namespace: str, pod: str, container: str, path: str = "/") -> List[FileEntry]:
        self._check_oc()
        status = self.get_pod_status(namespace, pod)
        if status != "Running":
            raise BackendError(f"Pod '{pod}' is not Running (status: {status}). Cannot browse its file system.")

        result = _run(
            ["oc", "exec", pod, "-c", container, "-n", namespace, "--", "ls", "-la", path]
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "not found" in stderr.lower():
                raise BackendError(f"Path not found inside container: {path}")
            if "forbidden" in stderr.lower() or "permission" in stderr.lower():
                raise BackendError(f"Permission denied executing into '{pod}/{container}'.")
            raise BackendError(stderr or f"Could not list '{path}' in '{pod}/{container}'.")
        return parse_ls_la(result.stdout)

    # -- file copy ---------------------------------------------------

    def copy_from_pod(self, namespace: str, pod: str, container: str, remote_path: str, local_path: str) -> None:
        self._check_oc()
        # Same Windows drive-letter-colon problem as DockerBackend - see
        # _split_local_path_for_cp for the explanation. This is the exact
        # fix for "error: one of src or dest must be a local file
        # specification" when the local path looks like C:/Users/....
        cwd, filename = _split_local_path_for_cp(local_path)
        result = _run(
            ["oc", "cp", f"{namespace}/{pod}:{remote_path}", filename, "-c", container],
            timeout=180,
            cwd=cwd,
        )
        if result.returncode == 0:
            return

        cp_error = result.stderr.strip() or f"oc cp failed with exit code {result.returncode}."

        # oc cp runs tar inside the pod to stream directories/files out;
        # minimal images often don't ship tar, which surfaces as a bare
        # "command terminated with exit code 255" with no further detail.
        # Fall back to a tar-free single-file transfer via `oc exec ... cat`.
        fallback = _run_binary(
            ["oc", "exec", pod, "-c", container, "-n", namespace, "--", "cat", remote_path]
        )
        if fallback.returncode == 0:
            with open(local_path, "wb") as fh:
                fh.write(fallback.stdout)
            return

        fallback_error = fallback.stderr.decode("utf-8", errors="replace").strip()
        raise BackendError(
            f"oc cp failed: {cp_error}\n"
            f"Fallback copy also failed: {fallback_error or 'unknown error'}\n\n"
            f"{_TAR_MISSING_HINT}"
        )

    def copy_to_pod(self, namespace: str, pod: str, container: str, local_path: str, remote_path: str) -> None:
        self._check_oc()
        cwd, filename = _split_local_path_for_cp(local_path)
        result = _run(
            ["oc", "cp", filename, f"{namespace}/{pod}:{remote_path}", "-c", container],
            timeout=180,
            cwd=cwd,
        )
        if result.returncode == 0:
            return

        cp_error = result.stderr.strip() or f"oc cp failed with exit code {result.returncode}."

        # Fallback: stream the local file's bytes in via `oc exec -i ...
        # sh -c "cat > path"`, which doesn't need tar in the pod either.
        with open(local_path, "rb") as fh:
            data = fh.read()
        fallback = _run_binary(
            ["oc", "exec", "-i", pod, "-c", container, "-n", namespace, "--", "sh", "-c", f"cat > '{remote_path}'"],
            input_bytes=data,
        )
        if fallback.returncode == 0:
            return

        fallback_error = fallback.stderr.decode("utf-8", errors="replace").strip()
        raise BackendError(
            f"oc cp failed: {cp_error}\n"
            f"Fallback copy also failed: {fallback_error or 'unknown error'}\n\n"
            f"{_TAR_MISSING_HINT}"
        )
