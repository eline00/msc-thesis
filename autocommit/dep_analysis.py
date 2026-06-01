"""Post-grouping dependency analysis using regex-based C# symbol detection.

After ddmin has committed atomic groups, analyse def-use relationships between
them and suggest an optimal commit ordering via topological sort.
"""

import re
import sys
from collections import deque

# Matches PascalCase identifiers (C# types, methods, properties follow this convention)
_PASCAL = re.compile(r'\b([A-Z][A-Za-z0-9_]*)\b')

# Matches camelCase identifiers used in added lines (length >= 2 to skip single-letter vars)
_CAMEL = re.compile(r'\b([a-z][A-Za-z0-9_]+)\b')

# Matches C# member declarations: class/interface/enum/struct/record keywords,
# or access-modifier-prefixed members where the declared name is PascalCase.
_DECL = re.compile(
    r'\b(?:class|interface|enum|struct|record)\s+([A-Z]\w*)'
    r'|(?:(?:public|private|protected|internal|static|virtual|override|'
    r'abstract|sealed|async|new|partial|readonly)\s+)+'
    r'(?:[\w<>\[\],\s]+\s)([A-Z]\w*)\s*[({<;=]'
)

# Matches camelCase field declarations (access-modifier prefixed) and local var declarations.
# Covers: "private readonly bool fromSeparator;" and "var separatorSeen = false;"
_CAMEL_DECL = re.compile(
    r'(?:(?:public|private|protected|internal|static|virtual|override|'
    r'abstract|sealed|async|new|partial|readonly)\s+)+'
    r'(?:[\w<>\[\],\s]+\s)([a-z]\w*)\s*[({;=]'
    r'|\bvar\s+([a-z]\w*)\s*='
)

# Well-known BCL / framework names that appear everywhere and produce false edges
_SKIP: frozenset[str] = frozenset({
    # PascalCase BCL / framework types
    'String', 'Boolean', 'Int32', 'Int64', 'Double', 'Float', 'Object', 'Void',
    'Task', 'List', 'Dictionary', 'IEnumerable', 'IList', 'IDictionary',
    'Exception', 'Nullable', 'Action', 'Func', 'Type', 'Array', 'Tuple',
    'Console', 'StringBuilder', 'Stream', 'File', 'Path', 'Environment',
    'Result', 'Value', 'Key', 'Index', 'Item', 'Name', 'Id', 'Data',
    # camelCase C# keywords and ubiquitous locals that produce noise
    'true', 'false', 'null', 'this', 'base', 'var', 'new', 'void',
    'get', 'set', 'add', 'remove',
    'result', 'value', 'key', 'index', 'item', 'name', 'data', 'text',
    'count', 'size', 'length', 'error', 'message', 'args', 'param',
    'type', 'obj', 'arg', 'val', 'str', 'ret', 'res', 'tmp', 'temp',
})

# Drop symbols defined across more than this many groups (same logic as Roslyn approach)
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
    group_patches: list[tuple[str, str]],
) -> list[tuple[int, int, str]]:
    """
    Build directed def-use edges between committed groups.

    Returns list of (from_idx, to_idx, symbol) where group[from_idx] defines
    the symbol and group[to_idx] uses it.
    """
    defs = [_defined_symbols(text) for _, text in group_patches]
    uses = [_used_symbols(text) for _, text in group_patches]

    # Filter out symbols defined in too many groups — common names create noise
    spread: dict[str, int] = {}
    for sym_set in defs:
        for sym in sym_set:
            spread[sym] = spread.get(sym, 0) + 1

    edges: list[tuple[int, int, str]] = []
    for i, defs_i in enumerate(defs):
        for j, uses_j in enumerate(uses):
            if i == j:
                continue
            for sym in defs_i:
                if sym in uses_j and spread.get(sym, 0) <= _MAX_SYMBOL_SPREAD:
                    edges.append((i, j, sym))
    return edges


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

    # Cycle detected — append remaining nodes in original order
    if len(order) < n:
        order.extend(i for i in range(n) if i not in set(order))

    return order
