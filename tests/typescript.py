"""The frontend's TypeScript and JavaScript, read by the repository's guards: which files there
are, and their syntax trees, parsed with Vite's own parser from the frontend's packages. Not a
test module."""

from __future__ import annotations

import json
import operator
import subprocess
from pathlib import Path

from tests.hang_guard import HANG_GUARD

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
# The parser's language for each kind of source the guards read.
SOURCES = {
    ".ts": "ts", ".mts": "ts", ".cts": "ts", ".tsx": "tsx",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "jsx",
}  # fmt: skip
# Vite's own parser, from the frontend's packages: each source given on stdin as
# {name: [language, source]}, as ESTree, with offsets in UTF-16 code units.
PARSE_TS = """
import { parseAst } from "vite";
let input = "";
for await (const chunk of process.stdin) input += chunk;
const trees = {};
for (const [name, [lang, source]] of Object.entries(JSON.parse(input))) {
  try {
    trees[name] = parseAst(source, { lang });
  } catch (error) {
    throw new Error(`${name}: ${error.message}`);
  }
}
process.stdout.write(JSON.stringify(trees));
"""


def parse_ts(sources: dict[str, str], cwd: Path = FRONTEND) -> dict[str, dict]:
    """The ESTree of each source, by name, parsed as its suffix says, TypeScript where it
    says none. Raises when any does not parse, or the parser cannot be run."""
    given = {n: [SOURCES.get(Path(n).suffix, "ts"), s] for n, s in sources.items()}
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", PARSE_TS],
        cwd=cwd, input=json.dumps(given), capture_output=True, text=True, timeout=HANG_GUARD,
    )  # fmt: skip
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return json.loads(proc.stdout)


def ts_scanned(root: Path = FRONTEND) -> list[str]:
    """Every source of a kind in SOURCES under ``root`` that git does not ignore, relative
    to it, whatever its name."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."],
        cwd=root, check=True, capture_output=True,
    ).stdout.decode().split("\0")  # fmt: skip
    return sorted(r for r in listed if Path(r).suffix in SOURCES and (root / r).is_file())


def module_numbers(tree: dict) -> dict[str, float]:
    """The numbers a module's top-level constants hold, exported or not, where each is a number
    or arithmetic of numbers and constants before it."""
    found: dict[str, float] = {}

    def value(node: dict) -> float | None:
        kind = node.get("type")
        if kind == "ParenthesizedExpression":
            return value(node["expression"])
        if kind == "Literal" and type(node.get("value")) in (int, float):
            return node["value"]
        if kind == "Identifier":
            return found.get(node["name"])
        if kind == "BinaryExpression" and node["operator"] in OPERATORS:
            left, right = value(node["left"]), value(node["right"])
            if left is not None and right is not None:
                return OPERATORS[node["operator"]](left, right)
        return None

    for node in tree["body"]:
        if node["type"] == "ExportNamedDeclaration":
            node = node.get("declaration") or {}
        if node.get("type") == "VariableDeclaration":
            for given in node["declarations"]:
                number = value(given["init"] or {})
                if given["id"]["type"] == "Identifier" and number is not None:
                    found[given["id"]["name"]] = number
    return found


OPERATORS = {
    "+": operator.add, "-": operator.sub, "*": operator.mul, "/": operator.truediv,
}  # fmt: skip
