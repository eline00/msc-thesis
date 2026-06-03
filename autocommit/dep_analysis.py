"""Post-grouping dependency analysis using regex-based C# symbol detection."""

import re
import sys
from collections import deque

# Matches PascalCase identifiers
_PASCAL = re.compile(r'\b([A-Z][A-Za-z0-9_]*)\b')

# Matches camelCase identifiers
_CAMEL = re.compile(r'\b([a-z][A-Za-z0-9_]+)\b')

# Matches C# declarations
_DECL = re.compile(
    r'\b(?:class|interface|enum|struct|record)\s+([A-Z]\w*)'
    r'|(?:(?:public|private|protected|internal|static|virtual|override|'
    r'abstract|sealed|async|new|partial|readonly)\s+)+'
    r'(?:[\w<>\[\],\s]+\s)([A-Z]\w*)\s*[({<;=]'
)

# Matches camelCase declarations and local var declarations
_CAMEL_DECL = re.compile(
    r'(?:(?:public|private|protected|internal|static|virtual|override|'
    r'abstract|sealed|async|new|partial|readonly)\s+)+'
    r'(?:[\w<>\[\],\s]+\s)([a-z]\w*)\s*[({;=]'
    r'|\bvar\s+([a-z]\w*)\s*='
)

# Well-known names that appear everywhere and produce false edges
_SKIP: frozenset[str] = frozenset({
    # PascalCase types
    'String', 'Boolean', 'Int32', 'Int64', 'Double', 'Float', 'Object', 'Void',
    'Task', 'List', 'Dictionary', 'IEnumerable', 'IList', 'IDictionary',
    'Exception', 'Nullable', 'Action', 'Func', 'Type', 'Array', 'Tuple',
    'Console', 'StringBuilder', 'Stream', 'File', 'Path', 'Environment',
    'Result', 'Value', 'Key', 'Index', 'Item', 'Name', 'Id', 'Data',
    # camelCase C# keywords and locals that produce noise
    'true', 'false', 'null', 'this', 'base', 'var', 'new', 'void',
    'typeof', 'nameof', 'sizeof', 'default', 'delegate',
    'get', 'set', 'add', 'remove', 'result', 'value', 'key', 'index', 
    'item', 'name', 'data', 'text', 'count', 'size', 'length', 'error', 
    'errors', 'message', 'args', 'param', 'type', 'obj', 'arg', 'val', 
    'str', 'ret', 'res', 'tmp', 'temp',
    # Common test variable names
    'sut', 'expected', 'actual', 'options', 'parser', 'result',
    # Common tokenizer / parser variable names
    'tokens', 'token', 'values', 'separator', 'sequence', 'state',
    'nothing', 'exploded', 'errors', 'input', 'output',
})

_MAX_SYMBOL_SPREAD = 3


def _added_lines(patch_text: str) -> list[str]:
    return [
        line[1:]
        for line in patch_text.splitlines()
        if line.startswith('+') and not line.startswith('+++')
    ]


def _defined_symbols(patch_text: str) -> set[str]:
    names: set[str] = set()
    for line in _added_lines(patch_text):
        for m in _DECL.finditer(line):
            name = m.group(1) or m.group(2)
            if name and len(name) >= 3 and name not in _SKIP:
                names.add(name)
        for m in _CAMEL_DECL.finditer(line):
            name = m.group(1) or m.group(2)
            if name and len(name) >= 3 and name not in _SKIP:
                names.add(name)
    return names


def _used_symbols(patch_text: str) -> set[str]:
    names: set[str] = set()
    for line in _added_lines(patch_text):
        for name in _PASCAL.findall(line):
            if name not in _SKIP:
                names.add(name)
        for name in _CAMEL.findall(line):
            if name not in _SKIP:
                names.add(name)
    return names


def build_dep_graph(
    hunk_patches: list[tuple[str, str]],
) -> list[tuple[int, int, str]]:
    """
    Build def-use edges between committed groups.

    Returns list of (from_idx, to_idx, symbol) where group[from_idx] defines
    the symbol and group[to_idx] uses it.
    """
    defs = [_defined_symbols(text) for _, text in hunk_patches]
    uses = [_used_symbols(text) for _, text in hunk_patches]

    # Filter out symbols defined in too many groups, common names create noise
    spread: dict[str, int] = {}
    for definitions in defs:
        for definition in definitions:
            spread[definition] = spread.get(definition, 0) + 1

    edges: list[tuple[int, int, str]] = []
    for i, defs_i in enumerate(defs):
        for j, uses_j in enumerate(uses):
            if i == j:
                continue
            # Exclude symbols that patch j also defines (those appearances are declarations, not real uses of patch i's definition)
            real_uses_j = uses_j - defs[j]
            for definition in defs_i:
                if definition in real_uses_j and spread.get(definition, 0) <= _MAX_SYMBOL_SPREAD:
                    edges.append((i, j, definition))
    return edges


def hunk_clusters(hunk_paths: list[str]) -> list[list[str]]:
    """ Run dep analysis on individual hunk files and return ordered clusters."""
    
    from pathlib import Path as _Path
    from collections import defaultdict as _defaultdict

    patches = [(_Path(p).stem, _Path(p).read_text()) for p in hunk_paths]
    edges = build_dep_graph(patches)
    order = topological_order(len(patches), edges)
    order_pos = {idx: rank for rank, idx in enumerate(order)}

    parent = list(range(len(patches)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            if order_pos[px] <= order_pos[py]:
                parent[py] = px
            else:
                parent[px] = py

    for from_idx, to_idx, _ in edges:
        union(from_idx, to_idx)

    # Group indices by component root
    components: dict[int, list[int]] = _defaultdict(list)
    for i in range(len(patches)):
        components[find(i)].append(i)

    # Sort members within each component by topo order, then sort components
    items: list[tuple[int, list[str]]] = []
    for members in components.values():
        sorted_members = sorted(members, key=lambda i: order_pos[i])
        earliest = order_pos[sorted_members[0]]
        items.append((earliest, [hunk_paths[i] for i in sorted_members]))

    items.sort(key=lambda x: x[0])
    return [paths for _, paths in items]


def topological_order(n: int, edges: list[tuple[int, int, str]]) -> list[int]:
    """Return group indices in topologically sorted commit order (Kahn's algorithm)."""
    in_degree = [0] * n
    graph: list[list[int]] = [[] for _ in range(n)]
    seen_pairs: set[tuple[int, int]] = set()

    for from_idx, to_idx, _ in edges:
        pair = (from_idx, to_idx)
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            graph[from_idx].append(to_idx)
            in_degree[to_idx] += 1

    queue: deque[int] = deque(i for i in range(n) if in_degree[i] == 0)
    order: list[int] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for neighbour in graph[node]:
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)

    # Cycle detection: if we couldn't include all nodes, add the remaining ones in any order
    if len(order) < n:
        order.extend(i for i in range(n) if i not in set(order))

    return order
