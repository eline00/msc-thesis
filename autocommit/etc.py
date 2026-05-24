import argparse
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from autocommit.commit_message import generate as _generate_commit_message

SCRIPT_DIR = Path(__file__).parent
BUILD_CMD = "dotnet build --no-restore"
LOGGING_DIR = SCRIPT_DIR / "logging"
METRICS_LOG = LOGGING_DIR / "metrics.log"
RESULTS_DIR = SCRIPT_DIR / "results"
RUNS_CSV = RESULTS_DIR / "runs.csv"
GROUPS_CSV = RESULTS_DIR / "groups.csv"
STATE_FILE = SCRIPT_DIR / "run_state.json"

RUNS_HEADER = [
    "run_id", "timestamp", "approach", "repo", "original_branch",
    "tangled_sha", "total_hunks", "groups_produced",
    "total_invocations", "total_duration_ms", "build_cmd", "notes",
]
GROUPS_HEADER = ["run_id", "group_num", "hunk_count", "hunks"]

# Global state for cleanup
_original_branch: str = ""
_tangled_sha: str = ""
_log_file = None  # file handle for run.log

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
                # Preserve \r for CRLF repositories; only strip the trailing \n
                current_fh.write(raw_line.rstrip("\n") + "\n")

    if current_fh:
        current_fh.close()


def count_hunks(patch_file: Path) -> int:
    """Return the number of hunks in a patch file."""
    if not patch_file.exists():
        return 0
    return sum(1 for line in patch_file.read_text().splitlines() if line.startswith("@@"))


# endregion

# region Logging
def _init_log(mode: str = "a") -> None:
    global _log_file
    if _log_file:
        _log_file.close()
    LOGGING_DIR.mkdir(parents=True, exist_ok=True)
    _log_file = open(LOGGING_DIR / "run.log", mode)

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
    approach: str,
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
            run_id, timestamp, approach, repo, original_branch,
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

# region State persistence
def _load_state() -> dict:
    if not STATE_FILE.exists():
        log_error("No active run state found. Run with --setup first.")
        sys.exit(1)
    return json.loads(STATE_FILE.read_text())


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))

# endregion

# region Steps

def step_setup(test_name: str | None, approach: str = "programmatic") -> None:
    """--setup: Create detangling branch, save the tangled state, initialise run dirs."""
    global _original_branch, _tangled_sha

    if STATE_FILE.exists():
        log_error(
            "A run_state.json already exists — a run may already be in progress.\n"
            "  Finish it with --merge, or delete autocommit/run_state.json to start fresh."
        )
        sys.exit(1)

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
    git_run("checkout", "-b", "detangling")
    log_info("Created and switched to 'detangling' branch.")

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

    git_run("add", "-N", ".")
    if not git_output("diff", "-U0"):
        log_warning("No changes found.")
        git_run("checkout", _original_branch)
        git_run("branch", "-d", "detangling")
        return

    git_diff_to_file(SCRIPT_DIR / "full.patch")
    _init_log("w")
    log_info("Full patch saved to autocommit/full.patch")

    log_info("Creating temporary tangled commit...")
    git_run("add", "-A")
    git_run("commit", "-m", "tangled changes")
    _tangled_sha = git_output("rev-parse", "HEAD")
    log_info(f"Tangled SHA: {_tangled_sha}")
    git_run("reset", "--hard", "HEAD~1")
    log_success("Reset to clean state. Working tree is clean.")

    run_id = time.strftime("%Y%m%d_%H%M%S")
    run_dir = (test_dir / "runs" / run_id) if test_dir else (SCRIPT_DIR / "runs" / run_id)
    (run_dir / "hunks").mkdir(parents=True, exist_ok=True)
    (run_dir / "groups").mkdir(parents=True, exist_ok=True)
    METRICS_LOG.write_text("")

    total_hunk_count = count_hunks(SCRIPT_DIR / "full.patch")
    log_info(f"Total hunks in full patch: {total_hunk_count}")
    metrics_event("RUN_START", f"total_hunks={total_hunk_count},build_cmd={BUILD_CMD}")

    hunks_dir = run_dir / "hunks"
    hunks_dir.mkdir(parents=True, exist_ok=True)
    split_hunks(SCRIPT_DIR / "full.patch", hunks_dir)
    log_info(f"Split full patch into {total_hunk_count} hunk file(s).")

    _save_state({
        "run_id": run_id,
        "approach": approach,
        "tangled_sha": _tangled_sha,
        "original_branch": _original_branch,
        "test_name": test_name,
        "run_dir": str(run_dir),
        "total_hunk_count": total_hunk_count,
        "iteration": 0,
        "group_count": 0,
        "total_invocations": 0,
        "total_duration_ms": 0,
        "committed_groups": [],
        "hunks_dir": str(hunks_dir),
        "prev_remaining_hunk_count": None,
        "pending_hunk_paths": [],
        "iter_start_ms": None,
        "last_found_group": None,
        "last_invocations": 0,
        "last_iter_duration_ms": 0,
        "llm_proposed_groups": None,
        "llm_group_cursor": 0,
    })
    log_success(f"Setup complete. Run ID: {run_id}")
    log_info("Next: run with --split-hunks")


def step_split_hunks() -> None:
    """--split-hunks: Determine remaining hunks from the initial split and save the list for this iteration."""
    global _original_branch, _tangled_sha

    state = _load_state()
    _original_branch = state["original_branch"]
    _tangled_sha = state["tangled_sha"]
    _init_log()

    iteration = state["iteration"] + 1
    state["iteration"] = iteration
    state["iter_start_ms"] = int(time.time() * 1000)

    hunks_dir = Path(state["hunks_dir"])
    committed_names = {
        Path(h).name
        for group in state["committed_groups"]
        for h in group
    }
    remaining = sorted(
        p for p in hunks_dir.glob("hunk_*.patch")
        if p.name not in committed_names
    )
    remaining_hunk_count = len(remaining)

    if remaining_hunk_count == 0:
        log_success("All changes have been grouped.")
        state["remaining_hunk_count"] = 0
        _save_state(state)
        log_info("Next: run with --merge to finish.")
        return

    prev = state["prev_remaining_hunk_count"]
    if prev is not None and remaining_hunk_count >= prev:
        log_warning(
            f"No progress — remaining hunk count did not decrease after last commit "
            f"({prev} → {remaining_hunk_count}). "
            f"Stopping with {remaining_hunk_count} unresolvable hunk(s)."
        )
        state["remaining_hunk_count"] = remaining_hunk_count
        state["stalled"] = True
        _save_state(state)
        return

    state["prev_remaining_hunk_count"] = remaining_hunk_count
    state["stalled"] = False

    log_info(f"── Iteration {iteration}: {remaining_hunk_count} hunk(s) remaining ──")
    metrics_event("ITER_START", f"iteration={iteration},pending={remaining_hunk_count}")

    state["remaining_hunk_count"] = remaining_hunk_count
    state["pending_hunk_paths"] = [str(p) for p in remaining]
    _save_state(state)
    log_info("Next: run with --find-group")


def _run_ddmin_subprocess(
    pending: list[str],
    committed_paths: list[str],
    iteration: int,
    append_log: bool = False,
) -> tuple[list[str] | None, int, int]:
    """
    Run group.py (ddmin) on *pending* and return (group, invocations, duration_ms).
    group is None if ddmin found nothing buildable.
    """
    iter_start = int(time.time() * 1000)
    invocations_log = LOGGING_DIR / f"iter_{iteration}_invocations.log"

    committed_args = ["--committed", *committed_paths, "--"] if committed_paths else []
    proc = subprocess.Popen(
        [sys.executable, "-m", "autocommit.group", BUILD_CMD, *committed_args, *pending],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stderr is not None and proc.stdout is not None

    stderr_lines: list[str] = []
    for line in proc.stderr:
        print(line, end="", file=sys.stderr, flush=True)
        if _log_file:
            _log_file.write(line)
            _log_file.flush()
        stderr_lines.append(line)
    stdout_output = proc.stdout.read()
    proc.wait()

    mode = "a" if append_log else "w"
    with open(invocations_log, mode) as f:
        f.write("".join(stderr_lines))

    invocations = sum(1 for line in stderr_lines if "test" in line)
    duration_ms = int(time.time() * 1000) - iter_start
    group = [line for line in stdout_output.splitlines() if line.strip()]

    if not group or proc.returncode != 0:
        return None, invocations, duration_ms
    return group, invocations, duration_ms


def step_find_group() -> None:
    """--find-group: Find a minimal buildable group using the configured approach."""
    global _original_branch, _tangled_sha

    state = _load_state()
    _original_branch = state["original_branch"]
    _tangled_sha = state["tangled_sha"]
    _init_log()

    pending = state.get("pending_hunk_paths")
    if not pending:
        log_error("No pending hunks in state. Run --split-hunks first.")
        sys.exit(1)

    iteration = state["iteration"]
    run_dir = Path(state["run_dir"])
    groups_dir = run_dir / "groups"
    committed_paths = [h for group in state["committed_groups"] for h in group]
    approach = state.get("approach", "programmatic")
    iter_start = state.get("iter_start_ms") or int(time.time() * 1000)

    # ── Programmatic: pure ddmin ──────────────────────────────────────────────
    if approach == "programmatic":
        group, invocations, iter_duration = _run_ddmin_subprocess(
            pending, committed_paths, iteration,
        )

    # ── LLM / Hybrid ─────────────────────────────────────────────────────────
    elif approach in ("llm", "hybrid"):
        from autocommit.llm_group import propose_groups
        from autocommit.group import git_apply, run_build
        from autocommit.group import git_revert as _revert

        hunks_dir = Path(state["hunks_dir"])
        invocations = 0

        # Make the LLM call to propose groups
        if state.get("llm_proposed_groups") is None:
            metrics_event("LLM_CALL_START", f"pending={len(pending)}")
            call_start = int(time.time() * 1000)
            proposed = propose_groups(pending)  # list[list[hunk_name]]
            call_ms = int(time.time() * 1000) - call_start
            state["llm_proposed_groups"] = proposed
            state["llm_group_cursor"] = 0
            metrics_event("LLM_CALL_END", f"groups={len(proposed)},duration_ms={call_ms}")
            _save_state(state)
            log_info(f"LLM proposed {len(proposed)} group(s): sizes={[len(g) for g in proposed]}")

        proposed = state["llm_proposed_groups"]
        cursor = state.get("llm_group_cursor", 0)
        pending_names = {Path(p).name for p in pending}
        group = None

        # Iterate through LLM-proposed groups until one is verified
        while cursor < len(proposed) and group is None:
            candidate_names = [n for n in proposed[cursor] if n in pending_names]
            cursor += 1
            state["llm_group_cursor"] = cursor
            _save_state(state)

            if not candidate_names:
                continue  # all hunks in this proposed group already committed

            candidate_paths = [str(hunks_dir / n) for n in candidate_names]
            log_info(f"Trying LLM group {cursor}/{len(proposed)}: "
                     f"{len(candidate_names)} hunk(s) — {', '.join(candidate_names)}")

            if approach == "hybrid":
                # ddmin finds the minimal buildable subset of the LLM-proposed group,
                # giving it a much smaller search space than the full pending set
                ddmin_group, ddmin_inv, _ = _run_ddmin_subprocess(
                    candidate_paths, committed_paths, iteration, append_log=(cursor > 1),
                )
                invocations += ddmin_inv
                if ddmin_group:
                    group = ddmin_group

            else:  # pure LLM: verify the proposed group as-is
                applied = git_apply(candidate_paths, committed=committed_paths)
                if not applied:
                    _revert(candidate_paths)
                    log_warning(f"LLM group {cursor} failed to apply — skipping")
                    continue
                built = run_build(BUILD_CMD)
                if not built:
                    _revert(candidate_paths)
                    log_warning(f"LLM group {cursor} failed build — skipping")
                    continue
                # Verified: leave changes applied so --commit-group can stage them
                group = candidate_paths

        # Hybrid fallback: LLM groups exhausted but hunks remain → programmatic ddmin
        if group is None and approach == "hybrid":
            log_info("LLM groups exhausted — falling back to programmatic ddmin on remaining hunks")
            fallback_group, fallback_inv, _ = _run_ddmin_subprocess(
                pending, committed_paths, iteration, append_log=True,
            )
            invocations += fallback_inv
            group = fallback_group

        iter_duration = int(time.time() * 1000) - iter_start

    else:
        log_error(f"Unknown approach '{approach}'. Use: programmatic, llm, or hybrid.")
        sys.exit(1)

    # ── Shared outcome handling ───────────────────────────────────────────────
    state["last_invocations"] = invocations
    state["last_iter_duration_ms"] = iter_duration

    if not group:
        remaining_hunk_count = state["remaining_hunk_count"]
        log_warning(f"No buildable group found for the remaining {remaining_hunk_count} hunk(s).")
        metrics_event(
            "ITER_FAILED",
            f"iteration={iteration},approach={approach},invocations={invocations},"
            f"duration_ms={iter_duration},remaining={remaining_hunk_count}",
        )
        state["last_found_group"] = None
        _save_state(state)
        return

    # Write the merged group patch file so it can be inspected before committing
    next_group_num = state["group_count"] + 1
    group_file = groups_dir / f"group_{next_group_num:04d}.patch"
    with open(group_file, "w") as out:
        for hunk_path in group:
            out.write(Path(hunk_path).read_text())

    group_names = ", ".join(Path(g).name for g in group)
    log_success(f"Found group {next_group_num} ({len(group)} hunk(s)) → {group_file}  [{group_names}]")

    state["last_found_group"] = group
    _save_state(state)
    log_info("Next: run with --commit-group (or inspect the group patch first)")


def step_commit_group() -> None:
    """--commit-group: Commit the group found by --find-group."""
    global _original_branch, _tangled_sha

    state = _load_state()
    _original_branch = state["original_branch"]
    _tangled_sha = state["tangled_sha"]
    _init_log()

    group = state.get("last_found_group")
    if not group:
        log_error("No group ready to commit. Run --find-group first.")
        sys.exit(1)

    iteration = state["iteration"]
    invocations = state["last_invocations"]
    iter_duration = state["last_iter_duration_ms"]
    group_count = state["group_count"] + 1
    group_names = ", ".join(Path(g).name for g in group)

    run_dir = Path(state["run_dir"])
    group_file = run_dir / "groups" / f"group_{group_count:04d}.patch"
    patch_content = group_file.read_text() if group_file.exists() else ""
    commit_msg = _generate_commit_message(patch_content, group_count, len(group), group_names)

    git_run("add", "-A")
    git_run("commit", "-m", commit_msg)

    state["group_count"] = group_count
    state["committed_groups"].append(group)
    state["total_invocations"] += invocations
    state["total_duration_ms"] += iter_duration
    state["last_found_group"] = None

    metrics_event(
        "ITER_GROUP",
        f"iteration={iteration},group={group_count},group_size={len(group)},"
        f"invocations={invocations},duration_ms={iter_duration},hunks={group_names}",
    )

    _save_state(state)
    log_success(f"Committed group {group_count}.")
    log_info("Next: run with --split-hunks for the next iteration, or --merge when done.")


def step_one_iteration() -> None:
    """--one-iteration: Run one full cycle: --split-hunks + --find-group + --commit-group."""
    step_split_hunks()

    state = json.loads(STATE_FILE.read_text())
    if state.get("remaining_hunk_count", 1) == 0:
        log_info("All changes grouped — run with --merge to finish.")
        return
    if state.get("stalled"):
        return

    step_find_group()

    state = json.loads(STATE_FILE.read_text())
    if not state.get("last_found_group"):
        return

    step_commit_group()


def step_merge() -> None:
    """--merge: Fast-forward the original branch over the detangling branch and write results."""
    global _original_branch, _tangled_sha

    state = _load_state()
    _original_branch = state["original_branch"]
    _tangled_sha = state["tangled_sha"]
    _init_log()

    group_count = state["group_count"]
    total_hunk_count = state["total_hunk_count"]
    committed_groups = state["committed_groups"]
    total_invocations = state["total_invocations"]
    total_duration_ms = state["total_duration_ms"]
    run_id = state["run_id"]

    metrics_event("RUN_END", f"groups={group_count}")
    log_success(f"Done: {group_count} group(s) produced from {total_hunk_count} hunk(s).")

    if group_count == 0:
        log_warning("No groups were produced — skipping merge.")
        git_run("checkout", _original_branch)
        git_run("branch", "-D", "detangling")
        STATE_FILE.unlink(missing_ok=True)
        return

    write_results(
        run_id=run_id,
        approach=state.get("approach", "programmatic"),
        tangled_sha=_tangled_sha,
        original_branch=_original_branch,
        total_hunks=total_hunk_count,
        groups=committed_groups,
        total_invocations=total_invocations,
        total_duration_ms=total_duration_ms,
    )

    git_run("checkout", _original_branch)
    if git_run("merge", "--ff-only", "detangling") != 0:
        log_error("Merge failed. The detangling branch has been left intact.")
        log_info(f"Inspect it with: git log {_original_branch}..detangling")
        return
    git_run("branch", "-D", "detangling")
    log_success(f"Moved {group_count} atomic commit(s) onto '{_original_branch}' and deleted 'detangling'.")

    STATE_FILE.unlink(missing_ok=True)

# endregion

# region Full automatic run

def run_all(test_name: str | None, approach: str = "programmatic") -> None:
    """Default: run all steps automatically end-to-end."""
    step_setup(test_name, approach=approach)

    while True:
        state = json.loads(STATE_FILE.read_text())

        step_split_hunks()

        state = json.loads(STATE_FILE.read_text())
        if state.get("remaining_hunk_count", 1) == 0:
            break
        if state.get("stalled"):
            break

        step_find_group()

        state = json.loads(STATE_FILE.read_text())
        if not state.get("last_found_group"):
            break

        step_commit_group()

    step_merge()

# endregion

# region Main

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="etc",
        description="ETC — Extract Test Commits. Splits staged changes into atomic, build-verified commits.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Step-by-step usage:\n"
            "  etc --setup [test_name]   Initialise a detangling run\n"
            "  etc --split-hunks         Determine remaining hunks and copy them for this iteration\n"
            "  etc --find-group          Run ddmin to find a minimal buildable group\n"
            "  etc --commit-group        Commit the group found by --find-group\n"
            "  etc --one-iteration       Run one full iteration (extract → test → commit)\n"
            "  etc --merge               Merge the detangling branch and write results\n"
            "\n"
            "Full automatic run (default):\n"
            "  etc [test_name]           Run all steps end-to-end\n"
        ),
    )
    parser.add_argument(
        "test_name", nargs="?",
        help="Test folder name under autocommit/tests/ (used with --setup or default run)",
    )
    parser.add_argument(
        "--approach", choices=["programmatic", "llm", "hybrid"], default="programmatic",
        help="Grouping strategy: programmatic (ddmin only), llm (LLM only), or hybrid (LLM + ddmin). "
             "Only used with --setup or a full run. Ignored by other steps (approach is read from state).",
    )

    steps = parser.add_mutually_exclusive_group()
    steps.add_argument(
        "--setup", action="store_true",
        help="Create the detangling branch, snapshot the tangled state, initialise run dirs",
    )
    steps.add_argument(
        "--split-hunks", dest="split_hunks", action="store_true",
        help="Determine remaining hunks from the initial split and copy them for this iteration",
    )
    steps.add_argument(
        "--find-group", dest="find_group", action="store_true",
        help="Run ddmin on the current hunk files to find a minimal buildable group",
    )
    steps.add_argument(
        "--commit-group", dest="commit_group", action="store_true",
        help="Commit the group identified by --find-group",
    )
    steps.add_argument(
        "--one-iteration", dest="one_iteration", action="store_true",
        help="Run one full iteration: --split-hunks then --find-group then --commit-group",
    )
    steps.add_argument(
        "--merge", action="store_true",
        help="Fast-forward the original branch over the detangling branch and write results",
    )

    args = parser.parse_args()

    if args.setup:
        step_setup(args.test_name, approach=args.approach)
    elif args.split_hunks:
        step_split_hunks()
    elif args.find_group:
        step_find_group()
    elif args.commit_group:
        step_commit_group()
    elif args.one_iteration:
        step_one_iteration()
    elif args.merge:
        step_merge()
    else:
        run_all(args.test_name, approach=args.approach)


if __name__ == "__main__":
    main()

# endregion
