"""
Pushes a completed run's JSON to the grant-runs repo on a per-client branch.

Set GRANT_RUNS_PATH env var to the local path of the grant-runs clone.
If not set, this module silently skips — the pipeline still works without it.
"""
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger("grant-agent")


def _slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:60]


def _run_git(args: list[str], cwd: str) -> tuple[int, str]:
    result = subprocess.run(
        ["git"] + args, cwd=cwd,
        capture_output=True, text=True,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def push_to_grant_runs(run_data: dict, client_name: str, grant_runs_path: str = "") -> bool:
    """
    Write run JSON to the grant-runs repo and push to origin.
    Returns True on success, False on skip/error.
    """
    path = grant_runs_path or os.getenv("GRANT_RUNS_PATH", "")
    if not path:
        return False

    repo = Path(path).expanduser().resolve()
    if not repo.exists():
        _log.warning(f"[git_manager] grant-runs path not found: {repo}")
        return False

    slug = _slug(client_name)
    branch = f"client/{slug}"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    try:
        # Fetch latest from origin (best-effort)
        _run_git(["fetch", "origin"], str(repo))

        # Check if branch already exists (locally or remotely)
        rc_local, _ = _run_git(["show-ref", "--verify", f"refs/heads/{branch}"], str(repo))
        rc_remote, _ = _run_git(["show-ref", "--verify", f"refs/remotes/origin/{branch}"], str(repo))

        if rc_local == 0:
            _run_git(["checkout", branch], str(repo))
            _run_git(["pull", "origin", branch, "--ff-only"], str(repo))
        elif rc_remote == 0:
            _run_git(["checkout", "-b", branch, f"origin/{branch}"], str(repo))
        else:
            # Fresh branch off main
            _run_git(["checkout", "main"], str(repo))
            _run_git(["checkout", "-b", branch], str(repo))

        # Write files
        client_dir = repo / "clients" / slug
        runs_dir = client_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)

        json_bytes = json.dumps(run_data, indent=2, ensure_ascii=False)
        (client_dir / "latest.json").write_text(json_bytes, encoding="utf-8")
        (runs_dir / f"{ts}.json").write_text(json_bytes, encoding="utf-8")

        # Commit and push
        _run_git(["add", f"clients/{slug}/"], str(repo))
        commit_msg = f"run: {client_name} — {ts}"
        rc, out = _run_git(["commit", "-m", commit_msg], str(repo))
        if rc != 0 and "nothing to commit" in out:
            _log.info(f"[git_manager] nothing new to commit for {client_name}")
        else:
            _run_git(["push", "-u", "origin", branch], str(repo))
            _log.info(f"[git_manager] pushed run to {branch}")

        # Return to main
        _run_git(["checkout", "main"], str(repo))
        return True

    except Exception as exc:
        _log.warning(f"[git_manager] failed to push run: {exc}")
        # Always try to return to main
        try:
            _run_git(["checkout", "main"], str(repo))
        except Exception:
            pass
        return False
