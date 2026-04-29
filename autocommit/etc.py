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
LOGGING_DIR = SCRIPT_DIR / "logging"
METRICS_LOG = LOGGING_DIR / "metrics.log"
RESULTS_DIR = SCRIPT_DIR / "results"
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
    """
    Split a unified diff into one file per hunk under hunks_dir.
    """

    diff_header: list[str] = []
    file_minus = ""
    file_plus = ""
    current_fh = None
    hunk_count = 0

    with open(patch_file, newline="") as f:
        for raw_line in f:
            line = raw_line.rstrip("\r\n")

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
    """Return the number of hunks in a patch file."""
    if not patch_file.exists():
        return 0
    return sum(1 for line in patch_file.read_text().splitlines() if line.startswith("@@"))


def remove_drift_hunks(patch_file: Path, full_patch: Path) -> int:
    """
    Remove position-drift hunks from a re-diff patch.
    Returns the number of hunks removed.
    """

    # Build content sets from the original tangled commit.
    inserted_in_original: set[str] = set()
    deleted_in_original: set[str] = set()
    for line in full_patch.read_text().splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            inserted_in_original.add(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            deleted_in_original.add(line[1:])

    text = patch_file.read_text()
    lines = text.splitlines(keepends=True)

    # Parse into file segments, each with a header block and a list of hunks.
    segments: list[dict] = []
    current_file: dict | None = None
    current_hunk: dict | None = None

    for line in lines:
        if line.startswith("diff --git"):
            current_file = {"header": [line], "hunks": []}
            segments.append(current_file)
            current_hunk = None
        elif current_file is None:
            pass
        elif line.startswith(("index ", "old mode", "new mode", "new file", "deleted file", "--- ", "+++ ")):
            current_file["header"].append(line)
            current_hunk = None
        elif line.startswith("@@"):
            current_hunk = {"header": line, "body": []}
            current_file["hunks"].append(current_hunk)
        elif current_hunk is not None:
            current_hunk["body"].append(line)

    # Cache of HEAD file contents, keyed by repo-relative path.
    _head_cache: dict[str, list[str]] = {}

    def _already_in_head(file_path: str, plus_lines: list[str]) -> bool:
        """Return True if plus_lines appear consecutively in the HEAD version of file_path."""
        import sys as _sys
        if file_path not in _head_cache:
            result = subprocess.run(
                ["git", "show", f"HEAD:{file_path}"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                _head_cache[file_path] = []
            else:
                _head_cache[file_path] = result.stdout.splitlines()
        head_lines = _head_cache[file_path]
        n = len(plus_lines)
        if n == 0 or len(head_lines) < n:
            return False
        found = any(head_lines[i:i + n] == plus_lines for i in range(len(head_lines) - n + 1))
        if not found and n >= 3:
            print(f"  [drift-dbg] NOT IN HEAD: {file_path!r} +{n} lines, first={plus_lines[0]!r}", file=_sys.stderr)
            # Check if any single line matches to detect encoding/whitespace issues
            for pl in plus_lines[:3]:
                matches = [i for i, hl in enumerate(head_lines) if hl == pl]
                print(f"  [drift-dbg]   line {plus_lines.index(pl)}: {len(matches)} exact matches in HEAD", file=_sys.stderr)
                if not matches and head_lines:
                    # Show repr of first head line with similar content
                    candidates = [hl for hl in head_lines if pl.strip() and pl.strip() in hl]
                    if candidates:
                        print(f"  [drift-dbg]   repr(plus)={pl!r}", file=_sys.stderr)
                        print(f"  [drift-dbg]   repr(head)={candidates[0]!r}", file=_sys.stderr)
        return found

    removed = 0
    output_parts: list[str] = []

    for seg in segments:
        # Extract file path from segment header ("+++ b/<path>" line).
        file_path = ""
        for h in seg["header"]:
            if h.startswith("+++ b/"):
                file_path = h[6:].rstrip("\r\n")
                break

        kept: list[dict] = []
        for hunk in seg["hunks"]:
            minus = [l[1:].rstrip("\r\n") for l in hunk["body"] if l.startswith("-")]
            plus  = [l[1:].rstrip("\r\n") for l in hunk["body"] if l.startswith("+")]

            # Pure move hunks where the same lines were removed and re-added at a different position are drift hunks and can be removed.
            if minus and plus and minus == plus:
                removed += 1
                continue

            if minus and all(line in inserted_in_original for line in minus) \
                    and not any(line in deleted_in_original for line in minus):
                removed += 1
                continue

            if not minus and plus and file_path and _already_in_head(file_path, plus):
                removed += 1
                continue

            kept.append(hunk)

        if kept:
            output_parts.extend(seg["header"])
            for hunk in kept:
                output_parts.append(hunk["header"])
                output_parts.extend(hunk["body"])

    patch_file.write_text("".join(output_parts))
    return removed

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
    LOGGING_DIR.mkdir(parents=True, exist_ok=True)
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

# region Main
def main() -> None:
    global _original_branch, _tangled_sha, _log_file

    # ----- Optional test argument -----
    test_name = sys.argv[1] if len(sys.argv) > 1 else None
    test_dir: Path | None = None
    if test_name:
        test_dir = SCRIPT_DIR / "tests" / test_name
        if not test_dir.exists():
            log_error(f"Test folder not found: {test_dir}")
            sys.exit(1)
        tangled_patch = test_dir / "tangled.patch"
        if not tangled_patch.exists():
            log_error(f"No tangled.patch in {test_dir}")
            sys.exit(1)

    _original_branch = git_output("branch", "--show-current")

    # ----- Step 1: Create new detangling branch -----
    git_run("checkout", "-b", "detangling")
    log_info("Created and switched to 'detangling' branch.")

    # ----- Step 1b: Apply tangled patch if running a test -----
    if test_dir:
        result = subprocess.run(
            ["git", "apply", "--unidiff-zero", str(tangled_patch)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log_error(f"Failed to apply tangled.patch:\n{result.stderr.strip()}")
            git_run("checkout", _original_branch)
            git_run("branch", "-D", "detangling")
            sys.exit(1)
        log_info(f"Applied tangled patch from test: {test_name}")

    # ----- Step 2: Check for changes -----
    git_run("add", "-N", ".")  # temporarily track untracked files so they appear in diff
    if not git_output("diff", "-U0"):
        log_warning("No changes found.")
        git_run("checkout", _original_branch)
        git_run("branch", "-d", "detangling")
        return

    # ----- Step 3: Save the full original patch for reference -----
    git_diff_to_file(SCRIPT_DIR / "full.patch")
    LOGGING_DIR.mkdir(parents=True, exist_ok=True)
    _log_file = open(LOGGING_DIR / "run.log", "w")
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
    run_id = time.strftime("%Y%m%d_%H%M%S")
    if test_dir:
        run_dir = test_dir / "runs" / run_id
    else:
        run_dir = SCRIPT_DIR / "runs" / run_id
    hunks_root = run_dir / "hunks"
    groups_dir = run_dir / "groups"
    hunks_root.mkdir(parents=True, exist_ok=True)
    groups_dir.mkdir(parents=True, exist_ok=True)
    METRICS_LOG.write_text("")

    total_hunk_count = count_hunks(SCRIPT_DIR / "full.patch")
    log_info(f"Total hunks in full patch: {total_hunk_count}")
    metrics_event("RUN_START", f"total_hunks={total_hunk_count},build_cmd={BUILD_CMD}")

    # ----- Step 6: Grouping loop -----
    log_info("Starting grouping with delta debugging...")
    group_count = 0
    iteration = 0
    total_invocations = 0
    total_duration_ms = 0
    committed_groups: list[list[str]] = []
    prev_remaining_hunk_count: int | None = None

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

        # Check if the hunk count did not decrease compared to the previous iteration
        if prev_remaining_hunk_count is not None and remaining_hunk_count >= prev_remaining_hunk_count:
            n_removed = remove_drift_hunks(remaining_patch, SCRIPT_DIR / "full.patch")
            if n_removed > 0:
                remaining_hunk_count = count_hunks(remaining_patch)
                log_info(
                    f"Removed {n_removed} drift hunk(s) "
                    f"(position-drift artifacts); {remaining_hunk_count} remaining."
                )
            if remaining_hunk_count >= prev_remaining_hunk_count:
                log_warning(
                    f"No progress — remaining hunk count did not decrease after last commit "
                    f"({prev_remaining_hunk_count} → {remaining_hunk_count}). "
                    f"Stopping with {remaining_hunk_count} unresolvable hunk(s)."
                )
                remaining_patch.unlink(missing_ok=True)
                break
        prev_remaining_hunk_count = remaining_hunk_count

        log_info(f"── Iteration {iteration}: {remaining_hunk_count} hunk(s) remaining ──")
        metrics_event("ITER_START", f"iteration={iteration},pending={remaining_hunk_count}")

        # Split new diff into individual hunk files for this iteration
        iter_hunks_dir = hunks_root / f"iter_{iteration:04d}"
        iter_hunks_dir.mkdir(parents=True, exist_ok=True)
        split_hunks(remaining_patch, iter_hunks_dir)
        pending = sorted(str(p) for p in iter_hunks_dir.glob("hunk_*.patch"))

        # Call group.py with all current hunks, streaming stderr live
        invocations_log = LOGGING_DIR / f"iter_{iteration}_invocations.log"
        grouping_process = subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / "group.py"), BUILD_CMD, *pending],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert grouping_process.stderr is not None and grouping_process.stdout is not None
        stderr_lines: list[str] = []
        for line in grouping_process.stderr:
            print(line, end="", file=sys.stderr, flush=True)
            if _log_file:
                _log_file.write(line)
                _log_file.flush()
            stderr_lines.append(line)
        stdout_output = grouping_process.stdout.read()
        grouping_process.wait()

        stderr_text = "".join(stderr_lines)
        invocations_log.write_text(stderr_text)

        result = subprocess.CompletedProcess(grouping_process.args, grouping_process.returncode, stdout_output, stderr_text)

        invocations = sum(1 for line in stderr_lines if "test" in line)

        group = [line for line in stdout_output.splitlines() if line.strip()]

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

    # ----- Step 7: Clean up iteration patch files -----
    #for p in SCRIPT_DIR.glob("remaining_iter_*.patch"):
        #p.unlink(missing_ok=True)

    # ----- Step 8: Results -----
    metrics_event("RUN_END", f"groups={group_count}")

    log_success(f"Done: {group_count} group(s) produced from {total_hunk_count} hunk(s).")

    # ----- Step 9: Move atomic commits to original branch and open PR -----
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
    
# endregion