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


def git_revert(patches: list[str]) -> None:
    """Revert previously applied patches in reverse order."""
    for patch in reversed(patches):
        subprocess.run(
            ["git", "apply", "--unidiff-zero", "-R", patch],
            capture_output=True,
        )


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
    primary: str,
    pending: list[str],
    build_cmd: str,
) -> list[str] | None:
    """
    Find the smallest buildable group.

    Steps:
      1. Try primary alone —> done if it builds.
      2. Try primary + all others —> if this fails, no solution exists, so return None.
      3. Pass the candidate list to delta_debug. delta_debug returns the
         minimal set needed.
      4. Apply the final group and leave it staged for etc.sh to commit.
    """
    others = [p for p in pending if p != primary]

    # --- 1. Primary alone ---
    log(f"Trying {name(primary)} alone...")
    if test_group([primary], build_cmd):
        log(f"{name(primary)} builds alone.")
        apply_group([primary])
        return [primary]

    if not others:
        log(f"{name(primary)} fails alone and there are no other hunks.")
        return None

    # --- 2. Upper-bound check ---
    # delta_debug requires that interesting_test(interesting_input) is True,
    # i.e. the full candidate list must already pass.
    log(f"{name(primary)} fails alone. "
        f"Checking upper bound (primary + all {len(others)} remaining hunk(s))...")
    if not test_group([primary] + others, build_cmd):
        log(f"Even primary + all pending fails. No buildable group for {name(primary)}.")
        return None

    # --- 3. Minimise companions via delta_debug ---
    log(f"Upper bound passes. Minimising companions via delta_debug "
        f"({len(others)} candidate(s))...")

    def predicate(companions: list[str]) -> bool:
        result = test_group([primary] + companions, build_cmd)
        log(f"    probe {len(companions)} hunk(s) -> {'PASS' if result else 'fail'}")
        return result

    minimal_companions = delta_debug(predicate, others)

    group = [primary] + minimal_companions
    log(f"Minimal group found: {names(group)} ({len(group)} hunk(s))")

    # --- 4. Apply final group (leave staged for etc.sh) ---
    if not apply_group(group):
        return None

    return group


# ------------------------------------------------------------------ #
#  Entry point                                                       #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "Usage: group.py <build_cmd> <primary_hunk> [pending_hunk ...]",
            file=sys.stderr,
        )
        sys.exit(2)

    build_cmd = sys.argv[1]
    primary   = sys.argv[2]
    pending   = sys.argv[3:]

    try:
        group = find_buildable_group(primary, pending, build_cmd)
    except KeyboardInterrupt:
        print("\n[group] Interrupted.", file=sys.stderr, flush=True)
        sys.exit(130)

    if group is None:
        sys.exit(1)

    for path in group:
        print(path)

    sys.exit(0)