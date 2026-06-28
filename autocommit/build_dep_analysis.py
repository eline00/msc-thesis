"""Build-based dependency grouping: pairwise build-probing over committed atoms.

An *atom* is a list of hunk-file paths. Atoms are ordered; atom B can only
depend on atoms committed before it. We detect hard build dependencies by
rebuilding each atom's prefix with one earlier atom removed and probing the
build oracle, then merge mutually-dependent atoms into connected components.
"""


def test_build_dependencies(
    atoms: list[list[str]],
    oracle,
) -> tuple[list[tuple[int, int]], int]:
    """Detect hard build dependencies between atoms via pairwise build-probing.

    atoms:  ordered list of atoms; each atom is a list of hunk-file paths.
    oracle: callable(combined_hunks: list[str]) -> bool, True if the set applies
            and builds, False otherwise (build-failure or apply-failure).

    For each atom B (index b) and each earlier atom A (index a < b), probe the
    set flatten(atoms[0..b]) - atoms[a]. If the oracle returns False, B has a
    hard build dependency on A; record edge (a, b).

    Returns (edges, invocations).
    """
    edges: list[tuple[int, int]] = []
    invocations = 0
    for b in range(len(atoms)):
        for a in range(b):
            a_hunks = set(atoms[a])
            combined = [
                h
                for idx in range(b + 1)
                for h in atoms[idx]
                if h not in a_hunks
            ]
            invocations += 1
            if not oracle(combined):
                edges.append((a, b))
    return edges, invocations


# Name mirrors group.py's test_group, but it is core logic, not a pytest test.
# Setting __test__ = False stops pytest from collecting it (it has required args)
# when it is imported into a test_*.py module.
test_build_dependencies.__test__ = False


def connected_components(n: int, edges: list[tuple[int, int]]) -> list[list[int]]:
    """Undirected connected components over atoms 0..n-1.

    Singletons are included. Each component's members are sorted ascending and
    components are ordered by their minimum member index.
    """
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)

    for a, b in edges:
        union(a, b)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    components = [sorted(members) for members in groups.values()]
    components.sort(key=lambda members: members[0])
    return components


# ---------------------------------------------------------------------------
# Post-merge reconstruction: map re-diffed hunks back to the recorded groups.
#
# After a run is merged, run_state.json is gone, so the hunk→atom membership
# must be rebuilt. We re-diff base..HEAD to obtain hunks with clean-base line
# numbers, then match each one to the group_*.patch that contains it. Matching
# is by content signature (file + body lines), which is invariant under the
# line-number adjustment applied to the group patches. The same signature
# function is applied to both sides, so any consistent parsing still matches.
# ---------------------------------------------------------------------------

Signature = tuple[str, tuple[str, ...]]


def iter_hunk_signatures(patch_text: str) -> list[Signature]:
    """Return a (file, body-lines) signature for every hunk in a patch.

    body-lines are the added/removed content lines (excluding the +++/--- file
    headers); the @@ header is excluded so line numbers do not affect the
    signature. Works for single-hunk patches (returns a 1-element list) and
    multi-hunk patches.
    """
    sigs: list[Signature] = []
    current_file = ""
    sig_file = ""
    body: list[str] | None = None

    def flush() -> None:
        nonlocal body
        if body is not None:
            sigs.append((sig_file, tuple(body)))
            body = None

    for line in patch_text.splitlines():
        if line.startswith("--- a/") or line.startswith("--- /dev/null"):
            flush()
        elif line.startswith("+++ b/") or line.startswith("+++ /dev/null"):
            flush()
            current_file = line[6:].strip() if line.startswith("+++ b/") else "/dev/null"
        elif line.startswith("diff --git"):
            flush()
        elif line.startswith("@@"):
            flush()
            body = []
            sig_file = current_file
        elif body is not None:
            if line.startswith("+") and not line.startswith("+++"):
                body.append(line)
            elif line.startswith("-") and not line.startswith("---"):
                body.append(line)

    flush()
    return sigs


def assign_hunks_to_groups(
    raw_hunks: list[tuple[str, Signature]],
    group_signatures: list[list[Signature]],
) -> list[list[str]]:
    """Partition raw hunk paths into atoms according to the recorded groups.

    raw_hunks:        (path, signature) for each re-diffed hunk.
    group_signatures: per group, the ordered signatures of its hunks.

    Returns one atom (list of raw-hunk paths) per group, in group order. Each
    group claims raw hunks whose signature matches its own; duplicate
    signatures are claimed FIFO so counts line up. Group signatures with no
    matching raw hunk are skipped.
    """
    from collections import defaultdict, deque

    pool: dict[Signature, deque[str]] = defaultdict(deque)
    for path, sig in raw_hunks:
        pool[sig].append(path)

    atoms: list[list[str]] = []
    for sigs in group_signatures:
        atom: list[str] = []
        for sig in sigs:
            if pool[sig]:
                atom.append(pool[sig].popleft())
        atoms.append(atom)
    return atoms
