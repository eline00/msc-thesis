#!/usr/bin/env python3
"""
Groups hunks into the smallest buildable set for ETC.

Uses delta_debug() from the ddmin library (andrewchambers/ddmin-python,
which implements the ddmin algorithm from Zeller & Hildebrandt, 2002) to find
the minimal set of hunks required alongside the primary hunk.

Arguments:
    build_cmd     : shell command to check the build
    primary_hunk  : the hunk currently being processed
    pending_hunks : remaining unprocessed hunks (candidates to pair with primary)
"""

import subprocess
import sys

import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from ddmin import delta_debug


# ------------------------------------------------------------------ #
#  Logging                                                           #
# ------------------------------------------------------------------ #

def log(msg: str) -> None:
    """Write progress to stderr."""
    print(f"  [group] {msg}", file=sys.stderr, flush=True)


def name(path: str) -> str:
    return path.split("/")[-1]

def names(paths: list[str]) -> str:
    return " + ".join(name(p) for p in paths)


# ------------------------------------------------------------------ #
#  Git helpers                                                       #
# ------------------------------------------------------------------ #

def git_apply(patches: list[str], check_only: bool = False) -> bool:
    """
    Apply one or more patch files.

    --unidiff-zero is used because all patches are generated
    with 'git diff -U0' (zero context lines).

    Returns True on success, False on failure.
    """
    cmd = ["git", "apply", "--unidiff-zero"]
    if check_only:
        cmd.append("--check")
    cmd.extend(patches)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"git apply failed: {result.stderr.strip()}")
    return result.returncode == 0


def _files_in_patches(patches: list[str]) -> list[str]:
    """Return deduplicated list of files modified by the given patches."""
    seen: dict[str, None] = {}
    for patch in patches:
        with open(patch) as f:
            for line in f:
                if line.startswith("+++ b/"):
                    seen[line[6:].strip()] = None
    return list(seen)


def git_revert(patches: list[str]) -> None:
    """Restore files touched by patches back to their index state.

    Tracked files are restored via 'git restore'; new files created by
    patches (not yet in the index) are simply deleted.
    """
    files = _files_in_patches(patches)
    if not files:
        return

    tracked = set(
        subprocess.run(
            ["git", "ls-files", "--"] + files,
            capture_output=True, text=True,
        ).stdout.splitlines()
    )

    restore = [f for f in files if f in tracked]
    if restore:
        result = subprocess.run(
            ["git", "restore", "--"] + restore,
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log(f"git restore failed: {result.stderr.strip()}")

    for f in files:
        if f not in tracked and os.path.exists(f):
            os.remove(f)


# ------------------------------------------------------------------ #
#  Build check                                                       #
# ------------------------------------------------------------------ #

def run_build(build_cmd: str) -> bool:
    """Run the build command. Returns True if exit code is 0."""
    result = subprocess.run(build_cmd, shell=True, capture_output=True)
    return result.returncode == 0


# ------------------------------------------------------------------ #
#  Probing vs. final application                                     #
# ------------------------------------------------------------------ #

def test_group(hunks: list[str], build_cmd: str) -> bool:
    """Apply hunks, run the build, revert."""
    applied: list[str] = []
    for hunk in hunks:
        if git_apply([hunk]):
            applied.append(hunk)
        else:
            git_revert(applied)
            return False
    result = run_build(build_cmd)
    git_revert(applied)
    return result


def apply_group(hunks: list[str]) -> bool:
    """Apply hunks and leave them applied."""
    applied: list[str] = []
    for hunk in hunks:
        if git_apply([hunk]):
            applied.append(hunk)
        else:
            log("ERROR: Failed to apply final group —> reverting.")
            git_revert(applied)
            return False
    return True


# ------------------------------------------------------------------ #
#  Core                                                              #
# ------------------------------------------------------------------ #

def find_buildable_group(
    pending: list[str],
    build_cmd: str,
) -> list[str] | None:

    def predicate(companions: list[str]) -> bool:
        result = test_group(companions, build_cmd)
        log(f"    test {len(companions)} hunk(s) -> {f'PASS: {names(companions)}' if result else 'fail'}")
        return result

    group = delta_debug(predicate, pending)

    # --- 4. Apply final group (leave staged for etc.sh) ---
    if not apply_group(group):
        return None

    return group


# ------------------------------------------------------------------ #
#  Entry point                                                       #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: group.py <build_cmd> [pending_hunk ...]",
            file=sys.stderr,
        )
        sys.exit(2)

    build_cmd = sys.argv[1]
    pending   = sys.argv[2:]

    try:
        group = find_buildable_group(pending, build_cmd)
    except KeyboardInterrupt:
        print("\n[group] Interrupted.", file=sys.stderr, flush=True)
        sys.exit(130)

    if group is None:
        sys.exit(1)

    for path in group:
        print(path)

    sys.exit(0)