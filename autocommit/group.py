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


def adjusted_patch(hunks: list[dict], committed: list[dict] | None = None) -> str:
    """
    Adjust line numbers in the headers of the given hunks.
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

    # Index committed hunks by file
    committed_by_file: dict[str, list[dict]] = {}
    for c in (committed or []):
        f = c['file']
        if f not in committed_by_file:
            committed_by_file[f] = []
        committed_by_file[f].append(c)

    # Adjust both old_start (committed delta) and new_start (intra-group delta)
    for f in file_order:
        committed_in_file = sorted(committed_by_file.get(f, []), key=lambda c: c['old_start'])
        line_offset = 0
        for h in sorted(hunks_by_file[f], key=lambda h: h['old_start']):
            committed_delta = sum(
                c['new_count'] - c['old_count']
                for c in committed_in_file
                if c['old_start'] < h['old_start']
            )
            h['adjusted_old_start'] = h['old_start'] + committed_delta
            base = h['adjusted_old_start'] + (1 if h['old_count'] == 0 else 0)
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
            old_s = h['adjusted_old_start']
            new_s = h['adjusted_new_start']

            old_side = f"-{old_s}" if old_c == 1 else f"-{old_s},{old_c}"
            new_side = f"+{new_s}" if new_c == 1 else f"+{new_s},{new_c}"
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
def git_apply(patches: list[str], check_only: bool = False, committed: list[str] | None = None) -> bool:
    """Apply one or more patch files."""
    if not patches:
        return True

    parsed = [parse_hunk(p) for p in patches]
    parsed_committed = [parse_hunk(p) for p in (committed or [])]
    patch_content = adjusted_patch(parsed, parsed_committed)

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
    committed: list[str] | None = None,
) -> list[str] | None:

    # test if the entire group is buildable before starting delta debugging
    def build_test(companions: list[str]) -> bool:
        applied = git_apply(companions, committed=committed)
        if not applied:
            git_revert(companions)
            log(f"    test {len(companions)} hunk(s) -> fail")
            return False
        result = run_build(build_cmd)
        git_revert(companions)
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
        if not git_apply(group, committed=committed):
            log("Failed to apply final group.")
            return None
    except Exception as e:
        log(f"Failed to apply final group with exception: {e}")
        return None

    return group



def find_buildable_group_clustered(
    clusters: list[list[str]],
    build_cmd: str,
    committed: list[str] | None = None,
) -> list[list[str]] | None:
    """
    Find the minimal buildable set of dep-graph clusters using delta debugging.

    Each cluster is treated as an atomic unit — ddmin will not split within a
    cluster.  Returns the minimal list of clusters whose combined hunks apply
    and build, with those hunks left applied in the working tree.
    """
    def build_test(candidate_clusters: list[list[str]]) -> bool:
        hunks = [h for cluster in candidate_clusters for h in cluster]
        applied = git_apply(hunks, committed=committed)
        if not applied:
            git_revert(hunks)
            log(f"    test {len(candidate_clusters)} cluster(s) ({len(hunks)} hunk(s)) -> fail")
            return False
        result = run_build(build_cmd)
        git_revert(hunks)
        log(f"    test {len(candidate_clusters)} cluster(s) ({len(hunks)} hunk(s)) -> {'PASS: ' + names([h for c in candidate_clusters for h in c]) if result else 'fail'}")
        return result

    try:
        result_clusters = delta_debug(build_test, clusters)
    except Exception as e:
        log(f"Delta debugging (clustered) failed: {e}")
        return None

    all_hunks = [h for c in result_clusters for h in c]
    try:
        if not git_apply(all_hunks, committed=committed):
            log("Failed to apply final clustered group.")
            return None
    except Exception as e:
        log(f"Failed to apply final clustered group: {e}")
        return None

    return result_clusters


if __name__ == "__main__":
    import argparse as _argparse
    import json as _json

    _parser = _argparse.ArgumentParser(prog="group")
    _parser.add_argument("build_cmd")
    _parser.add_argument("--committed", nargs="*", default=[], metavar="HUNK")
    _parser.add_argument(
        "--clusters-json", metavar="FILE",
        help="JSON file listing dep-graph clusters [[hunk,...], ...]; enables cluster-ddmin mode",
    )
    _parser.add_argument("pending", nargs="*")
    _args = _parser.parse_args()

    committed = _args.committed or None

    try:
        if _args.clusters_json:
            with open(_args.clusters_json) as _f:
                _clusters = _json.load(_f)
            result = find_buildable_group_clustered(_clusters, _args.build_cmd, committed)
            if result is None:
                sys.exit(1)
            for _cluster in result:
                for _path in _cluster:
                    print(_path)
        else:
            result = find_buildable_group(_args.pending, _args.build_cmd, committed)
            if result is None:
                sys.exit(1)
            for _path in result:
                print(_path)
    except KeyboardInterrupt:
        print("\n[group] Interrupted.", file=sys.stderr, flush=True)
        sys.exit(130)

    sys.exit(0)