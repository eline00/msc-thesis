"""
Tests for adjusted_patch() in group.py.

Each test builds minimal parsed-hunk dicts (as parse_hunk would return) and
checks that adjusted_patch() produces the right @@ header values.

Hunk dict fields used:
    file        — repo-relative path string
    old_start   — line number in the original (pre-change) file
    old_count   — lines removed (0 = insertion-only hunk)
    new_start   — line number in the patched file (from original patch; gets replaced)
    new_count   — lines added (0 = deletion-only hunk)
    suffix      — text after @@ ... @@ on the header line
    hunk_match  — regex match object from parse_hunk; None means pass-through
    content     — full text of the hunk file

We use hunk_match=None (pass-through) for every test hunk so adjusted_patch
returns the content unchanged for hunks that lack a parseable header.  To
verify numeric output we build real content strings with a fake @@ header and
check that the header in the output was replaced correctly.
"""

import re
import pytest
from autocommit.group import adjusted_patch


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_hunk(file: str, old_start: int, old_count: int, new_count: int,
               new_start: int = 1) -> dict:
    """
    Build a minimal hunk dict the same shape as parse_hunk() returns.
    The content field contains a real @@ header so adjusted_patch can
    patch it in-place; we capture the match object the same way parse_hunk
    does.
    """
    suffix = ""
    old_side = f"-{old_start}" if old_count == 1 else f"-{old_start},{old_count}"
    new_side = f"+{new_start}" if new_count == 1 else f"+{new_start},{new_count}"
    header_line = f"@@ {old_side} {new_side} @@{suffix}\n"

    # Minimal file header + hunk body
    body_lines = ["-old_line\n"] * old_count + ["+new_line\n"] * new_count
    content = (
        f"diff --git a/{file} b/{file}\n"
        f"--- a/{file}\n"
        f"+++ b/{file}\n"
        + header_line
        + "".join(body_lines)
    )

    m = re.search(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)', content, re.MULTILINE)

    return {
        'path': f'/fake/{file}',
        'file': file,
        'old_start': old_start,
        'old_count': old_count,
        'new_start': new_start,
        'new_count': new_count,
        'suffix': suffix,
        'content': content,
        'hunk_match': m,
    }


def _parse_header(patch_text: str) -> list[tuple[int, int, int, int]]:
    """Return list of (old_start, old_count, new_start, new_count) from all @@ headers."""
    results = []
    for m in re.finditer(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', patch_text, re.MULTILINE):
        old_start = int(m.group(1))
        old_count = int(m.group(2)) if m.group(2) is not None else 1
        new_start = int(m.group(3))
        new_count = int(m.group(4)) if m.group(4) is not None else 1
        results.append((old_start, old_count, new_start, new_count))
    return results


# ---------------------------------------------------------------------------
# baseline: no committed hunks — matches pre-existing behaviour
# ---------------------------------------------------------------------------

def test_single_hunk_no_committed():
    h = _make_hunk("foo.cs", old_start=10, old_count=2, new_count=5)
    patch = adjusted_patch([h])
    headers = _parse_header(patch)
    assert len(headers) == 1
    old_s, old_c, new_s, new_c = headers[0]
    assert old_s == 10
    assert old_c == 2
    assert new_s == 10   # base = old_start (old_count != 0), offset = 0
    assert new_c == 5


def test_two_hunks_same_file_no_committed():
    # h1 adds 3 lines (net +3); h2 should have new_start shifted by +3
    h1 = _make_hunk("foo.cs", old_start=10, old_count=2, new_count=5)  # net +3
    h2 = _make_hunk("foo.cs", old_start=20, old_count=1, new_count=1)  # net 0
    patch = adjusted_patch([h1, h2])
    headers = _parse_header(patch)
    assert len(headers) == 2
    # h1: old_start=10 unchanged, new_start=10+0=10
    assert headers[0] == (10, 2, 10, 5)
    # h2: old_start=20 unchanged, new_start=20+(5-2)=23
    assert headers[1] == (20, 1, 23, 1)


def test_two_hunks_different_files_no_committed():
    # Hunks in different files should not affect each other's new_start
    h1 = _make_hunk("foo.cs", old_start=10, old_count=1, new_count=4)  # net +3
    h2 = _make_hunk("bar.cs", old_start=10, old_count=1, new_count=1)
    patch = adjusted_patch([h1, h2])
    headers = _parse_header(patch)
    assert len(headers) == 2
    # h1: new_start = 10 + 0 = 10
    # h2: new_start = 10 + 0 = 10  (different file, no cross-file offset)
    assert headers[0] == (10, 1, 10, 4)
    assert headers[1] == (10, 1, 10, 1)


# ---------------------------------------------------------------------------
# committed hunks shift old_start of remaining hunks
# ---------------------------------------------------------------------------

def test_committed_hunk_shifts_old_start():
    # committed: adds 3 lines at position 10 → remaining hunk at 20 should shift to 23
    committed = _make_hunk("foo.cs", old_start=10, old_count=2, new_count=5)  # net +3
    remaining = _make_hunk("foo.cs", old_start=20, old_count=1, new_count=1)
    patch = adjusted_patch([remaining], committed=[committed])
    headers = _parse_header(patch)
    assert len(headers) == 1
    old_s, old_c, new_s, new_c = headers[0]
    assert old_s == 23  # 20 + 3
    assert new_s == 23  # base = adjusted old_start, intra-group offset = 0


def test_committed_hunk_after_remaining_no_shift():
    # committed hunk is *after* the remaining hunk — should not affect it
    committed = _make_hunk("foo.cs", old_start=30, old_count=1, new_count=4)  # net +3, but after
    remaining = _make_hunk("foo.cs", old_start=10, old_count=2, new_count=2)
    patch = adjusted_patch([remaining], committed=[committed])
    headers = _parse_header(patch)
    assert len(headers) == 1
    old_s, old_c, new_s, new_c = headers[0]
    assert old_s == 10   # no shift — committed is after
    assert new_s == 10


def test_committed_hunk_deletes_lines_shifts_old_start_down():
    # committed: removes 3 lines (net -3) at position 10 → remaining hunk at 20 shifts to 17
    committed = _make_hunk("foo.cs", old_start=10, old_count=4, new_count=1)  # net -3
    remaining = _make_hunk("foo.cs", old_start=20, old_count=1, new_count=1)
    patch = adjusted_patch([remaining], committed=[committed])
    headers = _parse_header(patch)
    assert len(headers) == 1
    old_s, _, new_s, _ = headers[0]
    assert old_s == 17   # 20 + (1 - 4) = 17
    assert new_s == 17


def test_multiple_committed_hunks_cumulative_shift():
    # Two committed hunks before remaining: net +3 + net +2 = +5
    c1 = _make_hunk("foo.cs", old_start=5,  old_count=1, new_count=4)   # net +3
    c2 = _make_hunk("foo.cs", old_start=10, old_count=1, new_count=3)   # net +2
    remaining = _make_hunk("foo.cs", old_start=20, old_count=1, new_count=1)
    patch = adjusted_patch([remaining], committed=[c1, c2])
    headers = _parse_header(patch)
    assert len(headers) == 1
    old_s, _, new_s, _ = headers[0]
    assert old_s == 25   # 20 + 3 + 2
    assert new_s == 25


def test_committed_in_different_file_no_cross_file_effect():
    committed = _make_hunk("bar.cs", old_start=5, old_count=1, new_count=10)   # net +9
    remaining = _make_hunk("foo.cs", old_start=20, old_count=1, new_count=1)
    patch = adjusted_patch([remaining], committed=[committed])
    headers = _parse_header(patch)
    assert len(headers) == 1
    old_s, _, new_s, _ = headers[0]
    assert old_s == 20   # no cross-file shift
    assert new_s == 20


def test_combined_committed_and_intra_group_offsets():
    # committed: net +3 at pos 5; two remaining hunks where the first also shifts the second
    committed = _make_hunk("foo.cs", old_start=5, old_count=1, new_count=4)   # net +3
    r1 = _make_hunk("foo.cs", old_start=15, old_count=1, new_count=3)         # net +2
    r2 = _make_hunk("foo.cs", old_start=25, old_count=2, new_count=2)         # net 0

    patch = adjusted_patch([r1, r2], committed=[committed])
    headers = _parse_header(patch)
    assert len(headers) == 2

    # r1: adjusted_old_start = 15 + 3 = 18; intra offset = 0; new_start = 18
    assert headers[0] == (18, 1, 18, 3)

    # r2: adjusted_old_start = 25 + 3 = 28; intra offset = +2 (from r1); new_start = 30
    assert headers[1] == (28, 2, 30, 2)


def test_insertion_only_hunk_old_count_zero():
    # old_count=0 means "insert before line N"; new_start formula adds 1
    remaining = _make_hunk("foo.cs", old_start=10, old_count=0, new_count=2)
    patch = adjusted_patch([remaining])
    headers = _parse_header(patch)
    assert len(headers) == 1
    old_s, old_c, new_s, new_c = headers[0]
    assert old_s == 10
    assert old_c == 0
    assert new_s == 11   # base = old_start + 1 when old_count == 0


def test_insertion_only_with_committed_shift():
    committed = _make_hunk("foo.cs", old_start=5, old_count=1, new_count=3)  # net +2
    remaining = _make_hunk("foo.cs", old_start=10, old_count=0, new_count=2)
    patch = adjusted_patch([remaining], committed=[committed])
    headers = _parse_header(patch)
    assert len(headers) == 1
    old_s, old_c, new_s, _ = headers[0]
    assert old_s == 12   # 10 + 2
    assert old_c == 0
    assert new_s == 13   # adjusted_old_start + 1


def test_empty_committed_list_same_as_none():
    h = _make_hunk("foo.cs", old_start=10, old_count=1, new_count=1)
    assert adjusted_patch([h], committed=[]) == adjusted_patch([h], committed=None)
