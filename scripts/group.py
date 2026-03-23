#!/usr/bin/env python3
"""
Groups hunks into buildable sets for ETC.

Arguments:
    build_cmd     : shell command to check the build
    primary_hunk  : the hunk currently being processed
    pending_hunks : remaining unprocessed hunks (candidates to pair with primary)
"""

import subprocess
import sys


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
    """
    Revert previously applied patches in reverse order.
    """
    for patch in reversed(patches):
        subprocess.run(
            ["git", "apply", "--unidiff-zero", "-R", patch],
            capture_output=True,
        )


# ------------------------------------------------------------------ #
#  Build check                                                         #
# ------------------------------------------------------------------ #

def run_build(build_cmd: str) -> bool:
    """Run the build command. Returns True if exit code is 0."""
    result = subprocess.run(build_cmd, shell=True, capture_output=True)
    return result.returncode == 0


# ------------------------------------------------------------------ #
#  Core                                                                #
# ------------------------------------------------------------------ #

def try_apply_group(hunks: list[str], build_cmd: str) -> bool:
    """
    Apply hunks in order, then run the build.
    """
    applied: list[str] = []

    for hunk in hunks:
        if git_apply([hunk]):
            applied.append(hunk)
        else:
            git_revert(applied)
            return False

    if run_build(build_cmd):
        return True
    else:
        git_revert(applied)
        return False


def find_buildable_group(
    primary: str,
    pending: list[str],
    build_cmd: str,
) -> list[str] | None:
    """
    Search for the smallest buildable group starting with 'primary'.
    
    Returns the buildable group, or None.
    """
    others = [p for p in pending if p != primary]

    # 1. Only primary
    log(f"Trying {name(primary)} alone...")
    if try_apply_group([primary], build_cmd):
        log(f"Hunk {name(primary)} succeeded alone.")
        return [primary]

    # 2. Pairs: primary + one other
    log(f"Hunk {name(primary)} failed alone. Trying pairs...")
    for candidate in others:
        log(f"  + {name(candidate)}...")
        if try_apply_group([primary, candidate], build_cmd):
            log("  Pair succeeded.")
            return [primary, candidate]

    # 3. Incremental growth: keep adding hunks until the build passes.
    if others:
        log(f"Pairs failed. Trying bigger groups...")
        accumulated = [primary]
        for candidate in others:
            accumulated.append(candidate)
            log(f"  Trying group of {len(accumulated)}: {name(candidate)} added...")
            if try_apply_group(accumulated, build_cmd):
                log(f"  Group of {len(accumulated)} succeeded.")
                return accumulated

    log(f"No buildable group found for {name(primary)}.")
    return None



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