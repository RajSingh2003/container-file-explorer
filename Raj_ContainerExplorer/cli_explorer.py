#!/usr/bin/env python3
"""
cli_explorer.py
----------------
Task 3 - Explore the Container File System via Command Line.

Connects to a running Docker container and prints its file system
structure (starting at a given path, default '/') as a readable tree.

Usage:
    python cli_explorer.py --container my_container --path /etc
    python cli_explorer.py -c my_container            # defaults to path '/'

Implementation notes:
    - Uses the shared DockerBackend from backends.py, which prefers the
      Docker SDK for Python (`docker.from_env()`) and transparently falls
      back to `docker exec <container> ls -la <path>` via subprocess if
      the SDK / daemon connection isn't available.
    - Errors (container not running, path not found, docker not
      installed, etc.) are caught and printed as a clean message rather
      than a raw traceback.
"""

import argparse
import sys

from backends import DockerBackend, BackendError, FileEntry


def print_tree(entries: list[FileEntry], root_path: str) -> None:
    """Print entries in the box-drawing tree style shown in the
    assignment's example output:

        /etc
        ├── hosts
        ├── hostname
        └── resolv.conf
    """
    print(root_path)
    for i, entry in enumerate(entries):
        connector = "└── " if i == len(entries) - 1 else "├── "
        suffix = "/" if entry.is_dir else ""
        print(f"{connector}{entry.name}{suffix}")


def print_details(entries: list[FileEntry]) -> None:
    """Optional verbose view: permissions, size, modified date."""
    if not entries:
        print("(empty directory)")
        return
    name_w = max(len(e.name) for e in entries) + 2
    print(f"{'PERMISSIONS':<11}  {'SIZE':>8}  {'MODIFIED':<12}  NAME")
    for e in entries:
        marker = "/" if e.is_dir else ""
        print(f"{e.permissions:<11}  {e.size:>8}  {e.modified:<12}  {e.name}{marker}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List the file system of a running Docker container."
    )
    parser.add_argument(
        "--container", "-c", required=True, help="Name or ID of the running container"
    )
    parser.add_argument(
        "--path", "-p", default="/", help="Path inside the container to list (default: /)"
    )
    parser.add_argument(
        "--long", "-l", action="store_true",
        help="Show permissions/size/modified date instead of the plain tree view",
    )
    args = parser.parse_args()

    backend = DockerBackend()

    try:
        entries = backend.list_dir(args.container, args.path)
    except BackendError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

    if args.long:
        print_details(entries)
    else:
        print_tree(entries, args.path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
