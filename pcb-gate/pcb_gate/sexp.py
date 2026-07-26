"""Minimal hand-rolled s-expression reader for .kicad_pcb / .kicad_dru.

Deliberately independent of `pcbnew` and `kiutils` (GNI-0283 / SVW-0034 GATE 5):
the overlap checker in overlap.py must not share a parser with anything KiCad
ships, so a parser bug can't be common-mode across both the CLI DRC and this
checker.

A parsed node is a plain Python list: `[tag, *children]`, where `tag` is the
first atom (a str) and each child is either an atom or another node (list).
Quoted strings parse to `QStr` (a `str` subclass) so `dumps()` can round-trip
them with their quotes; bare atoms (numbers, symbols like `yes`/`error`/`F.Cu`
without quotes) parse to plain `str`. This distinction matters: KiCad quotes
layer/net names but not numbers or keywords, and writing a bare atom back out
quoted (or vice versa) can change its meaning.
"""
from __future__ import annotations

from pathlib import Path

Node = list  # [Atom, *(Atom | Node)]


class QStr(str):
    """A string that was double-quoted in the source and must be re-quoted on dumps()."""

    __slots__ = ()


Atom = str  # QStr or plain str


class SexpError(ValueError):
    pass


def _tokenize(text: str):
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == ";":
            # Line comment - not part of the KiCad grammar, but harmless to skip.
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c in "()":
            yield c
            i += 1
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                raise SexpError(f"unterminated quoted string starting at offset {i}")
            yield ("str", QStr("".join(buf)))
            i = j + 1
            continue
        # bare atom: symbol, number, or keyword - runs until whitespace/paren
        j = i
        while j < n and text[j] not in " \t\r\n()":
            j += 1
        yield ("atom", text[i:j])
        i = j


def parse(text: str) -> list[Node]:
    """Parse `text` and return the list of top-level expressions."""
    stack: list[list] = [[]]
    for tok in _tokenize(text):
        if tok == "(":
            stack.append([])
        elif tok == ")":
            if len(stack) == 1:
                raise SexpError("unbalanced ')' with no matching '('")
            node = stack.pop()
            stack[-1].append(node)
        else:
            kind, value = tok
            stack[-1].append(value)
    if len(stack) != 1:
        raise SexpError(f"unbalanced '(' - {len(stack) - 1} unclosed")
    return stack[0]


def parse_file(path: str | Path) -> Node:
    """Parse a KiCad file and return its single top-level node."""
    text = Path(path).read_text(encoding="utf-8")
    top = parse(text)
    if len(top) != 1:
        raise SexpError(f"{path}: expected exactly one top-level expression, found {len(top)}")
    return top[0]


def tag(node) -> str | None:
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0]
    return None


def children(node: Node, name: str):
    """Yield direct child nodes of `node` whose tag equals `name`."""
    for item in node[1:]:
        if tag(item) == name:
            yield item


def child(node: Node, name: str) -> Node | None:
    """Return the first direct child node of `node` whose tag equals `name`."""
    for item in children(node, name):
        return item
    return None


def find_all(node: Node, name: str):
    """Recursively yield every node anywhere under `node` (node included) whose tag equals `name`."""
    if tag(node) == name:
        yield node
    for item in node[1:]:
        if isinstance(item, list):
            yield from find_all(item, name)


def text_of(node: Node | None) -> str | None:
    """Return the first atom argument of `node` (e.g. `(net_name "GND")` -> "GND")."""
    if node is None:
        return None
    for item in node[1:]:
        if isinstance(item, str):
            return item
    return None


def as_float(value) -> float:
    return float(value)


def _dump_atom(value: Atom) -> str:
    if isinstance(value, QStr):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return str(value)


def dumps(node) -> str:
    """Serialize a parsed node (or atom) back to KiCad-parseable text.

    Not a pretty-printer - whitespace doesn't matter to kicad-cli, only that
    tokens are separated and quoting round-trips. Used by canary.py to inject
    a single defect into a throwaway copy of a board without hand-rolling
    string surgery on the raw file per defect type.
    """
    if isinstance(node, list):
        return "(" + " ".join(dumps(item) for item in node) + ")"
    return _dump_atom(node)


def dump_file(path: str | Path, top_node: Node) -> None:
    Path(path).write_text(dumps(top_node) + "\n", encoding="utf-8")


def qstr(value: str) -> QStr:
    """Build a quoted atom, e.g. for a new node's net/layer name."""
    return QStr(value)
