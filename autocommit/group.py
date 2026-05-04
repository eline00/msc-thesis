import re
import subprocess
import sys

import os
from autocommit.ddmin import delta_debug



# region helpers
def log(msg: str) -> None:
    """Write progress to stderr."""
    print(f"  [group] {msg}", file=sys.stderr, flush=True)


def parse_hunk(hunk_path: str) -> dict:
    """Parse a single-hunk patch file and return its metadata."""
    with open(hunk_path) as f:
        content = f.read()

    file_match = re.search(r'^\+\+\+ b/(.+)$', content, re.MULTILINE)
    file = file_match.group(1).strip() if file_match else ""

    m = re.search(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)', content, re.MULTILINE)
    if not m:
        return {
            'path': hunk_path, 'file': file, 'content': content,
            'old_start': 0, 'old_count': 1, 'new_start': 0, 'new_count': 1,
            'suffix': '', 'hunk_match': None,
        }

    old_count = int(m.group(2)) if m.group(2) is not None else 1
    new_count = int(m.group(4)) if m.group(4) is not None else 1
    return {
        'path': hunk_path,
        'file': file,
        'old_start': int(m.group(1)),
        'old_count': old_count,
        'new_start': int(m.group(3)),
        'new_count': new_count,
        'suffix': m.group(5),
        'content': content,
        'hunk_match': m,
    }


def adjusted_patch(hunks: list[dict]) -> str:
    """ 
    Adjust the new line numbers in the headers of the given hunks 
    to account for missing hunks in the same file.
    """
    # Group and order hunks by file
    hunks_by_file: dict[str, list[dict]] = {}
    file_order: list[str] = []
    for h in hunks:
        f = h['file']
        if f not in hunks_by_file:
            hunks_by_file[f] = []
            file_order.append(f)
        hunks_by_file[f].append(h)

    # Find adjusted new_start for each hunk
    for f in file_order:
        line_offset = 0
        for h in sorted(hunks_by_file[f], key=lambda h: h['old_start']):
            base = h['old_start'] + (1 if h['old_count'] == 0 else 0)
            h['adjusted_new_start'] = base + line_offset
            line_offset += h['new_count'] - h['old_count']

    # Recreate patch
    parts: list[str] = []
    for f in file_order:
        for h in sorted(hunks_by_file[f], key=lambda h: h['old_start']):
            m = h['hunk_match']
            if m is None:
                parts.append(h['content'])
                continue

            old_c, new_c = h['old_count'], h['new_count']
            adj = h['adjusted_new_start']

            old_side = f"-{h['old_start']}" if old_c == 1 else f"-{h['old_start']},{old_c}"
            new_side = f"+{adj}"            if new_c == 1 else f"+{adj},{new_c}"
            new_header = f"@@ {old_side} {new_side} @@{h['suffix']}"

            adjusted = h['content'][:m.start()] + new_header + h['content'][m.end():]
            parts.append(adjusted)

    return "".join(parts)


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
    if not patches:
        return True

    parsed = [parse_hunk(p) for p in patches]
    patch_content = adjusted_patch(parsed)

    cmd = ["git", "apply", "--unidiff-zero", "--ignore-whitespace"]
    if check_only:
        cmd.append("--check")
    result = subprocess.run(cmd, input=patch_content, capture_output=True, text=True)
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


def find_buildable_group(
    pending: list[str],
    build_cmd: str,
) -> list[str] | None:

    # test if the entire group is buildable before starting delta debugging
    def build_test(companions: list[str]) -> bool:
        result = test_group(companions, build_cmd)
        log(f"    test {len(companions)} hunk(s) -> {f'PASS: {names(companions)}' if result else 'fail'}")
        return result

    # run delta debugging to find a minimal buildable group
    try:
        group = delta_debug(build_test, pending)
    except Exception as e:
        log(f"Delta debugging failed with exception: {e}")
        return None

    # apply group and return it if successful
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