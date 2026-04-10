import csv
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
BUILD_CMD = "dotnet build --no-restore"
APPROACH = "programmatic"
METRICS_LOG = SCRIPT_DIR / "metrics.log"
RESULTS_DIR = Path("results")
RUNS_CSV = RESULTS_DIR / "runs.csv"
GROUPS_CSV = RESULTS_DIR / "groups.csv"

RUNS_HEADER = [
    "run_id", "timestamp", "approach", "repo", "original_branch",
    "tangled_sha", "total_hunks", "groups_produced",
    "total_invocations", "total_duration_ms", "build_cmd", "notes",
]
GROUPS_HEADER = ["run_id", "group_num", "hunk_count", "hunks"]

# Global state for cleanup
_original_branch: str = ""
_tangled_sha: str = ""
_log_file = None  # file handle for run.log, opened in main()

# region Hunk splitting
def split_hunks(patch_file: Path, hunks_dir: Path) -> None:
    """Split a unified diff into one file per hunk under hunks_dir."""
    for f in hunks_dir.glob("hunk_*.patch"):
        f.unlink()

    diff_header: list[str] = []
    file_minus = ""
    file_plus = ""
    current_fh = None
    hunk_count = 0

    with open(patch_file, newline="") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")

            if line.startswith("diff --git"):
                if current_fh:
                    current_fh.close()
                    current_fh = None
                diff_header = [line]
                file_minus = ""
                file_plus = ""
            elif re.match(r"^(old|new) mode|^(deleted|new) file mode", line):
                diff_header.append(line)
            elif line.startswith("index "):
                diff_header.append(line)
            elif line.startswith("---"):
                file_minus = line
            elif line.startswith("+++"):
                file_plus = line
            elif line.startswith("@@"):
                hunk_count += 1
                hunk_path = hunks_dir / f"hunk_{hunk_count:04d}.patch"
                if current_fh:
                    current_fh.close()
                current_fh = open(hunk_path, "w", newline="")
                for h in diff_header:
                    current_fh.write(h + "\n")
                current_fh.write(file_minus + "\n")
                current_fh.write(file_plus + "\n")
                current_fh.write(line + "\n")
            elif current_fh is not None:
                current_fh.write(line + "\n")

    if current_fh:
        current_fh.close()


def count_hunks(patch_file: Path) -> int:
    if not patch_file.exists():
        return 0
    return sum(1 for line in patch_file.read_text().splitlines() if line.startswith("@@"))

# endregion

# region Logging
def _log_to_file(level: str, msg: str) -> None:
    if _log_file:
        ts = time.strftime("%H:%M:%S")
        _log_file.write(f"{ts} [{level}] {msg}\n")
        _log_file.flush()

def log_info(msg: str)    -> None: print(f"\033[1;34m[INFO]\033[0m {msg}");    _log_to_file("INFO",    msg)
def log_error(msg: str)   -> None: print(f"\033[1;31m[ERROR]\033[0m {msg}");   _log_to_file("ERROR",   msg)
def log_success(msg: str) -> None: print(f"\033[1;32m[SUCCESS]\033[0m {msg}"); _log_to_file("SUCCESS", msg)
def log_warning(msg: str) -> None: print(f"\033[1;33m[WARNING]\033[0m {msg}"); _log_to_file("WARNING", msg)

# endregion

# region Metrics
def metrics_event(event: str, data: str = "") -> None:
    ts = int(time.time() * 1000)
    METRICS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(METRICS_LOG, "a") as f:
        f.write(f"{ts}|{event}|{data}\n")
        
# endregion

# region Results CSV
def _ensure_csv(path: Path, header: list[str]) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(header)


def write_results(
    run_id: str,
    tangled_sha: str,
    original_branch: str,
    total_hunks: int,
    groups: list[list[str]],
    total_invocations: int,
    total_duration_ms: int,
) -> None:
    _ensure_csv(RUNS_CSV, RUNS_HEADER)
    _ensure_csv(GROUPS_CSV, GROUPS_HEADER)

    repo = os.path.basename(os.getcwd())
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    with open(RUNS_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            run_id, timestamp, APPROACH, repo, original_branch,
            tangled_sha, total_hunks, len(groups),
            total_invocations, total_duration_ms, BUILD_CMD, "",
        ])

    with open(GROUPS_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        for i, group in enumerate(groups, start=1):
            hunk_names = ", ".join(Path(h).name for h in group)
            writer.writerow([run_id, i, len(group), hunk_names])

    log_info(f"Results appended → {RUNS_CSV}, {GROUPS_CSV}")

# endregion

# region Cleanup
def cleanup(signum=None, frame=None) -> None:
    print()
    log_warning("Interrupted. Cleaning up...")
    metrics_event("INTERRUPTED")
    if _tangled_sha:
        subprocess.run(["git", "reset", "--hard", _tangled_sha], capture_output=True)
    if _original_branch:
        subprocess.run(["git", "checkout", _original_branch], capture_output=True)
        subprocess.run(["git", "branch", "-D", "detangling"], capture_output=True)
        log_info(f"Restored to branch '{_original_branch}'.")
    sys.exit(1)


signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

# endregion

# region Git helpers
def git_run(*args: str) -> int:
    """Run a git command and return its exit code."""
    return subprocess.run(["git", *args]).returncode


def git_output(*args: str) -> str:
    """Run a git command and return its stdout."""
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    return result.stdout.strip()


def git_diff_to_file(output_path: Path, *extra_args: str) -> None:
    """Run 'git diff' and write the output to output_path."""
    result = subprocess.run(
        ["git", "diff", "-U0", *extra_args], capture_output=True
    )
    output_path.write_bytes(result.stdout)

# endregion

# Main
def main() -> None:
    global _original_branch, _tangled_sha, _log_file

    _original_branch = git_output("branch", "--show-current")

    # ----- Step 1: Create new detangling branch -----
    git_run("checkout", "-b", "detangling")
    log_info("Created and switched to 'detangling' branch.")

    # ----- Step 2: Check for changes -----
    git_run("add", "-N", ".")  # temporarily track untracked files so they appear in diff
    if not git_output("diff", "-U0"):
        log_warning("No changes found.")
        git_run("checkout", _original_branch)
        git_run("branch", "-d", "detangling")
        return

    # ----- Step 3: Save the full original patch for reference -----
    git_diff_to_file(SCRIPT_DIR / "full.patch")
    _log_file = open(SCRIPT_DIR / "run.log", "w")
    log_info("Full patch saved to autocommit/full.patch")

    # ----- Step 4: Temporarily commit all changes as the tangled state, then reset -----
    log_info("Creating temporary tangled commit...")
    git_run("add", "-A")
    git_run("commit", "-m", "tangled changes")
    _tangled_sha = git_output("rev-parse", "HEAD")
    log_info(f"Tangled SHA: {_tangled_sha}")
    git_run("reset", "--hard", "HEAD~1")
    log_success("Reset to clean state. Working tree is clean.")

    # ----- Step 5: Initialise metrics log and patch dirs -----
    hunks_dir = SCRIPT_DIR / "hunks"
    groups_dir = SCRIPT_DIR / "groups"
    hunks_dir.mkdir(parents=True, exist_ok=True)
    groups_dir.mkdir(parents=True, exist_ok=True)
    METRICS_LOG.write_text("")

    total_hunk_count = count_hunks(SCRIPT_DIR / "full.patch")
    log_info(f"Total hunks in full patch: {total_hunk_count}")
    metrics_event("RUN_START", f"total_hunks={total_hunk_count},build_cmd={BUILD_CMD}")

    # ----- Step 6: Grouping loop -----
    log_info("Starting grouping with delta debugging...")
    run_id = time.strftime("%Y%m%d_%H%M%S")
    group_count = 0
    iteration = 0
    total_invocations = 0
    total_duration_ms = 0
    committed_groups: list[list[str]] = []

    while True:
        iteration += 1
        iter_start = int(time.time() * 1000)

        # Re-diff HEAD against tangled SHA
        remaining_patch = SCRIPT_DIR / f"remaining_iter_{iteration}.patch"
        git_diff_to_file(remaining_patch, "HEAD", _tangled_sha)

        remaining_hunk_count = count_hunks(remaining_patch)
        if remaining_hunk_count == 0:
            log_success("All changes have been grouped.")
            remaining_patch.unlink(missing_ok=True)
            break

        log_info(f"── Iteration {iteration}: {remaining_hunk_count} hunk(s) remaining ──")
        metrics_event("ITER_START", f"iteration={iteration},pending={remaining_hunk_count}")

        # Split new diff into individual hunk files
        split_hunks(remaining_patch, hunks_dir)
        pending = sorted(str(p) for p in hunks_dir.glob("hunk_*.patch"))

        # Call group.py with all current hunks
        invocations_log = SCRIPT_DIR / f"iter_{iteration}_invocations.log"
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "group.py"), BUILD_CMD, *pending],
            capture_output=True, text=True,
        )
        invocations_log.write_text(result.stderr)
        print(result.stderr, end="", file=sys.stderr)
        if _log_file and result.stderr:
            _log_file.write(result.stderr)
            _log_file.flush()

        invocations = sum(1 for line in result.stderr.splitlines() if "test" in line)

        group = [line for line in result.stdout.splitlines() if line.strip()]

        iter_end = int(time.time() * 1000)
        iter_duration = iter_end - iter_start
        total_invocations += invocations
        total_duration_ms += iter_duration

        # Handle failure
        if not group or result.returncode != 0:
            log_warning(f"No buildable group found for the remaining {remaining_hunk_count} hunk(s).")
            metrics_event(
                "ITER_FAILED",
                f"iteration={iteration},invocations={invocations},"
                f"duration_ms={iter_duration},remaining={remaining_hunk_count}",
            )
            break

        # Save the group as a merged patch file
        group_count += 1
        group_file = groups_dir / f"group_{group_count:04d}.patch"
        with open(group_file, "w") as out:
            for hunk_path in group:
                out.write(Path(hunk_path).read_text())

        group_names = ", ".join(Path(g).name for g in group)
        log_success(f"Group {group_count} ({len(group)} hunk(s)) → {group_file}  [{group_names}]")

        committed_groups.append(group)

        # Commit the group so HEAD advances and re-diff has only the remaining hunks
        git_run("add", "-A")
        git_run("commit", "-m", f"etc[{group_count}]: {len(group)} hunk(s) — {group_names}")

        metrics_event(
            "ITER_GROUP",
            f"iteration={iteration},group={group_count},group_size={len(group)},"
            f"invocations={invocations},duration_ms={iter_duration},hunks={group_names}",
        )

    # ----- Step 7: Results -----
    metrics_event("RUN_END", f"groups={group_count}")

    log_success(f"Done: {group_count} group(s) produced from {total_hunk_count} hunk(s).")

    # ----- Step 8: Move atomic commits to original branch and open PR -----
    if group_count == 0:
        log_warning("No groups were produced — skipping merge and PR.")
        return

    write_results(
        run_id=run_id,
        tangled_sha=_tangled_sha,
        original_branch=_original_branch,
        total_hunks=total_hunk_count,
        groups=committed_groups,
        total_invocations=total_invocations,
        total_duration_ms=total_duration_ms,
    )

    # Merge the detangling branch back into the original branch, which will move the atomic commits there
    git_run("checkout", _original_branch)
    if git_run("merge", "--ff-only", "detangling") != 0:
        log_error("Merge failed. The detangling branch has been left intact.")
        log_info(f"Inspect it with: git log {_original_branch}..detangling")
        return
    git_run("branch", "-D", "detangling")
    log_success(f"Moved {group_count} atomic commit(s) onto '{_original_branch}' and deleted 'detangling'.")

    # TODO: uncomment to test push + PR creation
    # log_info(f"Pushing '{_original_branch}' and opening pull request...")
    # if git_run("push", "-u", "origin", _original_branch) != 0:
    #     log_error("Push failed. PR not created.")
    #     return
    # pr_body = (
    #     f"Automatically generated by ETC.\n\n"
    #     f"This PR contains {group_count} atomic commit(s) extracted from {total_hunk_count} hunk(s).\n"
    #     f"Each commit is independently buildable."
    # )
    # result = subprocess.run(
    #     ["gh", "pr", "create",
    #      "--base", "master",
    #      "--head", _original_branch,
    #      "--title", f"etc: {group_count} atomic commit(s) from {total_hunk_count} hunk(s)",
    #      "--body", pr_body],
    #     capture_output=True, text=True,
    # )
    # if result.returncode == 0:
    #     pr_url = result.stdout.strip()
    #     log_success(f"PR created: {pr_url}")
    # else:
    #     log_error(f"gh pr create failed: {result.stderr.strip()}")


if __name__ == "__main__":
    main()
