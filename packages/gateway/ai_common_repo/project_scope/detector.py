"""project_id derivation.

Priority (design §9):
  1. explicit AI_COMMON_REPO_PROJECT_ID env
  2. normalized git remote url
  3. git root path
  4. cwd
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

from ..core import constants as C


def _run_git(args: list[str], cwd: Path) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                             text=True, timeout=3)
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _normalize_remote(url: str) -> str:
    url = url.strip()
    url = re.sub(r"^git@([^:]+):", r"\1/", url)         # git@host:org/repo -> host/org/repo
    url = re.sub(r"^[a-z]+://", "", url)                 # strip scheme
    url = re.sub(r"^[^@/]+@", "", url)                   # strip userinfo
    url = re.sub(r"\.git$", "", url)
    return url.rstrip("/")


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def detect_project_id(cwd: str | os.PathLike | None = None) -> str:
    explicit = os.environ.get(C.ENV_PROJECT_ID)
    if explicit:
        return explicit

    path = Path(cwd or os.getcwd()).resolve()

    remote = _run_git(["config", "--get", "remote.origin.url"], path)
    if remote:
        return f"repo:{_normalize_remote(remote)}"

    git_root = _run_git(["rev-parse", "--show-toplevel"], path)
    if git_root:
        return f"repo:local:{_short_hash(git_root)}"

    return f"cwd:{_short_hash(str(path))}"


def detect_git_info(cwd: str | os.PathLike | None = None) -> dict[str, str | None]:
    path = Path(cwd or os.getcwd()).resolve()
    return {
        "remote": _run_git(["config", "--get", "remote.origin.url"], path),
        "branch": _run_git(["rev-parse", "--abbrev-ref", "HEAD"], path),
        "commit_before": _run_git(["rev-parse", "HEAD"], path),
        "commit_after": None,
    }
