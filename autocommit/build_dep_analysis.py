def test_build_dependencies(
    atoms: list[list[str]],
    oracle,
) -> tuple[list[tuple[int, int]], int]:
    """Detect hard build dependencies between atoms via pairwise build-probing.

    atoms:  ordered list of atoms; each atom is a list of hunk-file paths.
    oracle: callable(combined_hunks: list[str]) -> bool, True if the set applies
            and builds, False otherwise (build-failure or apply-failure).

    Returns (edges, invocations).
    """
    edges: list[tuple[int, int]] = []
    invocations = 0
    n = len(atoms)
    for a in range(n):
        a_hunks = set(atoms[a])
        for b in range(a + 1, n):
            combined = [
                h
                for idx in range(b + 1)
                for h in atoms[idx]
                if h not in a_hunks
            ]
            invocations += 1
            if not oracle(combined):
                edges.append((a, b))
                break
    return edges, invocations


test_build_dependencies.__test__ = False


def connected_components(n: int, edges: list[tuple[int, int]]) -> list[list[int]]:
    """Undirected connected components over atoms 0..n-1. """
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


Signature = tuple[str, tuple[str, ...]]


def iter_hunk_signatures(patch_text: str) -> list[Signature]:
    """Return a (file, body-lines) signature for every hunk in a patch."""
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
    """Partition raw hunk paths into atoms according to the recorded groups."""
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


def reconstruct_atoms(
    hunk_files: list[str],
    group_files: list[str],
) -> tuple[list[list[str]], int]:
    """Rebuild ordered atoms from a run dir's hunk and group patch files.

    hunk_files:  original hunk patches (raw clean-base line numbers).
    group_files: committed group patches, in group order."""
    raw_hunks: list[tuple[str, Signature]] = []
    for path in hunk_files:
        with open(path) as f:
            sigs = iter_hunk_signatures(f.read())
        if sigs:
            raw_hunks.append((path, sigs[0]))

    group_signatures = []
    for gf in group_files:
        with open(gf) as f:
            group_signatures.append(iter_hunk_signatures(f.read()))

    atoms = assign_hunks_to_groups(raw_hunks, group_signatures)
    assigned = sum(len(a) for a in atoms)
    unmatched = len(raw_hunks) - assigned
    return atoms, unmatched
