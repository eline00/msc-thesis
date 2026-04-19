import subprocess
import sys

import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from ddmin import delta_debug



# region helpers
def log(msg: str) -> None:
    """Write progress to stderr."""
    print(f"  [group] {msg}", file=sys.stderr, flush=True)


def name(path: str) -> str:
    return path.split("/")[-1]

def names(paths: list[str]) -> str:
    return " + ".join(name(p) for p in paths)

def _files_in_patches(patches: list[str]) -> list[str]:
    """Return deduplicated list of files modified by the given patches."""
    seen: dict[str, None] = {}
    for patch in patches:
        with open(patch) as f:
            for line in f:
                if line.startswith("+++ b/"):
                    seen[line[6:].strip()] = None
    return list(seen)

# endregion

# region git
def git_apply(patches: list[str], check_only: bool = False) -> bool:
    """Apply one or more patch files."""
    
    cmd = ["git", "apply", "--unidiff-zero", "--ignore-whitespace"]
    if check_only:
        cmd.append("--check")
    cmd.extend(patches)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"git apply failed: {result.stderr.strip()}")
    return result.returncode == 0


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

# endregion

# region apply and build
def run_build(build_cmd: str) -> bool:
    """Run the build command. Returns True if exit code is 0."""
    result = subprocess.run(build_cmd, shell=True, capture_output=True)
    return result.returncode == 0


def test_group(hunks: list[str], build_cmd: str) -> bool:
    """Apply hunks, run the build, revert."""
    applied = git_apply(hunks)
    if not applied:
        git_revert(hunks)  # clean up any partial application
        return False
    result = run_build(build_cmd)
    git_revert(hunks)
    return result
# endregion


def _group_by_file(patches: list[str]) -> list[list[str]]:
    """Group patch files by their source file"""
    file_order: list[str] = []
    by_file: dict[str, list[str]] = {}
    for patch in patches:
        target = None
        with open(patch) as f:
            for line in f:
                if line.startswith("+++ b/"):
                    target = line[6:].strip()
                    break
        if target is None:
            target = patch  # fallback: treat as its own group
        if target not in by_file:
            file_order.append(target)
            by_file[target] = []
        by_file[target].append(patch)
    return [by_file[t] for t in file_order]


def find_buildable_group(
    pending: list[str],
    build_cmd: str,
) -> list[str] | None:

    file_groups = _group_by_file(pending)

    def build_test(selected_groups: list[list[str]]) -> bool:
        patches = [p for g in selected_groups for p in g]
        hunk_names = " + ".join(name(p) for g in selected_groups for p in g)
        result = test_group(patches, build_cmd)
        log(f"    test {len(selected_groups)} file(s) [{len(patches)} hunk(s)] -> {'PASS: ' + hunk_names if result else 'fail'}")
        return result

    # run delta debugging over file-groups to find a minimal buildable set
    try:
        winning_groups = delta_debug(build_test, file_groups)
    except Exception as e:
        log(f"Delta debugging failed with exception: {e}")
        return None

    # flatten winning file-groups back to a list of patch paths
    group = [p for g in winning_groups for p in g]

    # apply and return
    try:
        if not git_apply(group):
            log("Failed to apply final group.")
            return None
    except Exception as e:
        log(f"Failed to apply final group with exception: {e}")
        return None

    return group



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