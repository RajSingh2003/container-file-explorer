"""
version.py
----------
Single source of truth for the app's version number.

Bump this file, commit, then tag the release to match (e.g. bump to
"1.1.0" here, then `git tag v1.1.0 && git push --tags`). GitHub Actions
builds and publishes the installer for that tag automatically (see
.github/workflows/release.yml), and update_checker.py compares this
value against the latest GitHub release tag to tell users when a newer
version is available.
"""

__version__ = "1.0.1"
