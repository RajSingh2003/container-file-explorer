# Building and releasing an installer

This describes how to go from source code to a one-click Windows
installer that end users can download and run without installing
Python, pip, or anything else.

## The pieces

| File | Purpose |
|---|---|
| `version.py` | Single source of truth for the version number |
| `first_run.py` | First-launch setup wizard shown inside the app |
| `update_checker.py` | Checks GitHub Releases for a newer version |
| `installer.iss` | Inno Setup script - wraps the `.exe` into a proper installer wizard |
| `.github/workflows/release.yml` | Builds and publishes automatically when you push a version tag |

## One-time setup (you, not the end user)

1. Push this project to a GitHub repository.
2. Edit **`update_checker.py`** and set `GITHUB_REPO` to your actual
   `"owner/repo"` (it's a placeholder right now).
3. Edit **`installer.iss`** and set `MyAppPublisher` to your name/org.
4. That's it - the GitHub Actions workflow handles the rest from here on.

## Releasing a new version (every time you update the code)

```bash
# 1. Bump the version number
#    edit version.py: __version__ = "1.1.0"

git add .
git commit -m "Fix the tar-fallback timeout"

# 2. Tag it to match, and push
git tag v1.1.0
git push && git push --tags
```

That's the entire manual process. GitHub Actions then automatically:
1. Builds a standalone `ContainerExplorer.exe` with PyInstaller (bundles
   the Python interpreter itself - end users never install Python)
2. Wraps it into `ContainerExplorer-Setup.exe` with Inno Setup (a real
   installer wizard: Next → Next → Install → Finish, with Start Menu
   and Desktop shortcuts, and a working uninstaller)
3. Publishes that file to the repo's GitHub Releases

**You never manually zip or re-send a file again.** The end user's
download link never changes:

```
https://github.com/<you>/<repo>/releases/latest/download/ContainerExplorer-Setup.exe
```

That URL always serves whatever the newest tagged release is. Bookmark
it, put it on your LinkedIn post, put it in an email signature - it
never goes stale.

## How the end user finds out about updates

They don't have to check anything. `update_checker.py` runs quietly
~1 second after the app starts, compares the running version against
the latest GitHub release, and - only if a newer one exists - shows a
one-click "Update available, open download page?" prompt. If they're
already current, or they're offline, nothing is shown at all; the
check fails silently rather than nagging.

They can also check manually any time via **Help → Check for Updates
Now**, which always reports a result either way.

## Building locally (without waiting for GitHub Actions)

Only useful for testing the packaging itself before tagging a real
release. Requires Windows (PyInstaller's `--windowed` GUI build and
Inno Setup are both Windows-specific):

```powershell
pip install -r requirements.txt
pip install pyinstaller
pyinstaller --onefile --windowed --name ContainerExplorer gui_explorer.py
# -> dist\ContainerExplorer.exe

# then, with Inno Setup installed (https://jrsoftware.org/isinfo.php):
iscc installer.iss
# -> Output\ContainerExplorer-Setup.exe
```

## What the end user actually experiences

1. Clicks your one link → downloads `ContainerExplorer-Setup.exe`
2. Double-clicks it → a normal Windows installer wizard (no Python, no
   terminal, no README to read first)
3. Opens the app → if it's their first time, a small wizard checks
   whether `docker`/`oc` are on their PATH and gives them a direct
   download link for whatever's missing, then lets them continue
   regardless
4. Later, when you ship a fix → next time they open the app, they get
   a one-click prompt to grab the new version, from the same stable link
