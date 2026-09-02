"""Heuristic orphan-detection sweep: dead schema fields, dead functions/classes,
dead function parameters.

This is static, grep/AST-based analysis over the .py source tree. It is a
HEURISTIC, not a proof:
  - It will MISS dynamic access (getattr, locals(), string-built attribute/
    column names, kwargs forwarded through **kwargs, reflection).
  - It can produce FALSE POSITIVES for anything reached only through such
    dynamic means, or through a real external entry point this script
    doesn't know about (a cron-invoked script, a webhook handler, etc).
  - It can produce FALSE NEGATIVES when a field/function name is a common
    token that happens to appear elsewhere for an unrelated reason (the
    safer failure direction for a non-blocking tool).

Known, deliberate exceptions live in config/known_dormant.md -- anything
listed there is suppressed from the "new orphan" report. Everything else
that's orphaned is a visible flag, not a hard failure: run this manually,
or via tests/test_orphans.py in the regression suite, and use judgement.

Usage:
    python scripts/check_orphans.py            # full human-readable report
    python scripts/check_orphans.py --json      # machine-readable report
"""
from __future__ import annotations

import ast
import bisect
import json
import re
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = REPO_ROOT / "config" / "known_dormant.md"

EXCLUDED_DIR_PARTS = {".git", "__pycache__", "node_modules", ".venv", "venv"}

# Field-schema variable names to look for (module-level list assignments).
SCHEMA_VAR_RE = re.compile(r"^(FIELDS|FIELDNAMES|COLUMNS|CSV_FIELDS|SCHEMA)$")

# A quoted, identifier-shaped string literal: 'name' or "name".
_QUOTED_IDENT_RE = re.compile(r"""(['"])([A-Za-z_][A-Za-z0-9_]*)\1""")
# A keyword-argument-shaped token: name= (but not ==, >=, <=, !=).
_KWARG_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)")

TEST_FILE_RE = re.compile(r"(^|[/\\])test_[^/\\]*\.py$|(^|[/\\])tests[/\\]")


# ─────────────────────────────────────────────────────────────────────────
# Repo file discovery
# ─────────────────────────────────────────────────────────────────────────

def all_py_files() -> list[Path]:
    out = []
    for p in REPO_ROOT.rglob("*.py"):
        if any(part in EXCLUDED_DIR_PARTS for part in p.parts):
            continue
        out.append(p)
    return out


def src_py_files() -> list[Path]:
    src_dir = REPO_ROOT / "src"
    out = []
    for p in src_dir.rglob("*.py"):
        if any(part in EXCLUDED_DIR_PARTS for part in p.parts):
            continue
        out.append(p)
    return out


def is_test_file(p: Path) -> bool:
    rel = str(p.relative_to(REPO_ROOT)).replace("\\", "/")
    return bool(TEST_FILE_RE.search(rel)) or rel.startswith("tests/")


def rel(p: Path) -> str:
    return str(p.relative_to(REPO_ROOT)).replace("\\", "/")


# ─────────────────────────────────────────────────────────────────────────
# A whole-repo token index: identifier-shaped quoted strings and
# keyword-argument-shaped tokens, each mapped to every (file, lineno) they
# appear at. Built once, reused for both the field check and the
# function/class reference check.
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class TokenIndex:
    # token -> list of (rel_path, lineno)
    occurrences: dict = dc_field(default_factory=dict)

    def add(self, token: str, path: Path, lineno: int) -> None:
        self.occurrences.setdefault(token, []).append((rel(path), lineno))

    def sites(self, token: str) -> list:
        return self.occurrences.get(token, [])


def _line_offsets(text: str) -> list:
    offsets = [0]
    for m in re.finditer("\n", text):
        offsets.append(m.end())
    return offsets


def build_token_index(files: list) -> TokenIndex:
    idx = TokenIndex()
    for p in files:
        try:
            text = p.read_text(encoding="utf-8-sig")
        except Exception:
            continue
        offsets = _line_offsets(text)
        for m in _QUOTED_IDENT_RE.finditer(text):
            lineno = bisect.bisect_right(offsets, m.start())
            idx.add(m.group(2), p, lineno)
        for m in _KWARG_RE.finditer(text):
            lineno = bisect.bisect_right(offsets, m.start())
            idx.add(m.group(1), p, lineno)
    return idx


def build_identifier_index(files: list) -> TokenIndex:
    """Index real Python identifier references: bareword Name loads, `.attr`
    accesses (e.g. `financials.calculate_fund_state`), and import aliases
    (`from x import Name`). This is what a function/class call-site or a
    class instantiation actually looks like in source -- unlike fields
    (which live as string dict/CSV keys), functions and classes are
    referenced as real identifiers, not as quoted strings.
    """
    idx = TokenIndex()
    for p in files:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig"), filename=str(p))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                idx.add(node.id, p, node.lineno)
            elif isinstance(node, ast.Attribute):
                idx.add(node.attr, p, node.lineno)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    exposed = alias.asname or alias.name
                    idx.add(exposed, p, node.lineno)
                    # `from src.foo import bar` also makes `bar` referenceable
                    # under its original name even if aliased on import.
                    if alias.asname:
                        idx.add(alias.name, p, node.lineno)
    return idx


# ─────────────────────────────────────────────────────────────────────────
# 1. Orphaned schema fields
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class OrphanField:
    name: str
    file: str
    line: int
    schema_var: str


def _collect_string_literals(node: ast.AST) -> list:
    """All ast.Constant str leaves under `node`, in source order, with lineno."""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append((sub.value, sub.lineno))
    return out


def find_field_schemas() -> dict:
    """Returns {(schema_file, schema_var): [(field_name, lineno), ...]}."""
    schemas = {}
    for p in all_py_files():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig"), filename=str(p))
        except Exception:
            continue
        for node in tree.body:  # module-level only
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or not SCHEMA_VAR_RE.match(target.id):
                continue
            literals = _collect_string_literals(node.value)
            if not literals:
                continue
            schemas[(rel(p), target.id)] = literals
    return schemas


def check_orphan_fields(schemas: dict, idx: TokenIndex) -> list:
    orphans = []
    # Build the full set of (file, line) declaration sites across ALL schemas
    # first, so a field shared by two schemas (e.g. both trackers) isn't
    # penalised for "only" appearing at its own two declaration lines.
    declared_at: dict = {}
    for (schema_file, _var), literals in schemas.items():
        for name, lineno in literals:
            declared_at.setdefault(name, set()).add((schema_file, lineno))

    seen = set()
    for (schema_file, schema_var), literals in schemas.items():
        for name, lineno in literals:
            if (schema_file, name) in seen:
                continue
            seen.add((schema_file, name))
            sites = idx.sites(name)
            external = [s for s in sites if s not in declared_at.get(name, set())]
            if not external:
                orphans.append(OrphanField(name=name, file=schema_file, line=lineno,
                                            schema_var=schema_var))
    orphans.sort(key=lambda o: (o.file, o.line))
    return orphans


# ─────────────────────────────────────────────────────────────────────────
# 2. Orphaned top-level functions / classes (defined under src/)
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class OrphanDef:
    name: str
    kind: str  # "function" | "class"
    file: str
    line: int
    note: str = ""
    severity: str = "dead"  # "dead" (zero refs anywhere) | "insular" (own-file only)


DUNDER_RE = re.compile(r"^__[a-zA-Z0-9_]+__$")


def _main_guard_calls(tree: ast.Module) -> set:
    """Names called (or referenced) inside a top-level `if __name__ == '__main__':`
    block -- these are legitimate CLI entry points, not orphans."""
    calls = set()
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_main_guard = (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name) and test.left.id == "__name__"
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        )
        if not is_main_guard:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                calls.add(sub.id)
    return calls


def find_top_level_defs() -> list:
    """Returns [(file, name, kind, lineno, is_entry_point)]."""
    defs = []
    for p in src_py_files():
        if is_test_file(p):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig"), filename=str(p))
        except Exception:
            continue
        entry_names = _main_guard_calls(tree)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "function"
            elif isinstance(node, ast.ClassDef):
                kind = "class"
            else:
                continue
            if DUNDER_RE.match(node.name):
                continue
            is_entry = node.name in entry_names
            defs.append((p, node.name, kind, node.lineno, is_entry))
    return defs


def check_orphan_defs(defs: list, ident_idx: TokenIndex) -> list:
    """A def is flagged if:
      - it's a *public* (no leading underscore) name with zero references
        from any file OTHER than its own defining file, or
      - it's ANY name (public or private) with literally zero references
        anywhere at all (not even within its own file).
    Recognized __main__-guard entry points are always exempt.
    References are real Python identifier occurrences (calls, instantiations,
    imports, attribute access) -- see build_identifier_index().
    """
    orphans = []
    for p, name, kind, lineno, is_entry in defs:
        if is_entry:
            continue
        own_file = rel(p)
        sites = ident_idx.sites(name)
        # Exclude the def statement's own name-binding line from counting as
        # a "use" (class/function defs don't emit an ast.Name for their own
        # name, but be defensive in case of a same-name decorator/default).
        sites = [s for s in sites if not (s[0] == own_file and s[1] == lineno)]
        external = [s for s in sites if s[0] != own_file]
        total = sites

        if not total:
            orphans.append(OrphanDef(name=name, kind=kind, file=own_file, line=lineno,
                                      note="zero references anywhere", severity="dead"))
        elif not name.startswith("_") and not external:
            orphans.append(OrphanDef(name=name, kind=kind, file=own_file, line=lineno,
                                      note="only referenced within its own file",
                                      severity="insular"))
    orphans.sort(key=lambda o: (o.severity != "dead", o.file, o.line))
    return orphans


# ─────────────────────────────────────────────────────────────────────────
# 3. Orphaned function parameters (accepted but never used in the body)
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class OrphanParam:
    func_name: str
    param_name: str
    file: str
    line: int


def _used_names_in_body(fn_node) -> set:
    used = set()
    for sub in ast.walk(fn_node):
        if sub is fn_node:
            continue
        if isinstance(sub, ast.Name):
            used.add(sub.id)
        elif isinstance(sub, ast.arg):
            # Nested function/lambda parameter shadowing -- not a use of the
            # OUTER parameter, but harmless to include since we only check
            # for presence, and a shadowed name being "used" by an inner
            # scope's own parameter binding doesn't create a false negative
            # here (the outer name still needs its own Name-load reference).
            pass
    return used


def check_orphan_params(files: list) -> list:
    orphans = []
    for p in files:
        if is_test_file(p):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig"), filename=str(p))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if DUNDER_RE.match(node.name):
                continue
            args = node.args
            param_nodes = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
            if args.vararg:
                pass  # *args is conventionally allowed to go unused
            if args.kwarg:
                pass  # **kwargs likewise
            used = _used_names_in_body(node)
            for i, a in enumerate(param_nodes):
                pname = a.arg
                if pname in ("self", "cls"):
                    continue
                if pname.startswith("_"):
                    continue  # leading underscore = deliberately-unused convention
                if pname not in used:
                    orphans.append(OrphanParam(
                        func_name=node.name, param_name=pname,
                        file=rel(p), line=a.lineno,
                    ))
    orphans.sort(key=lambda o: (o.file, o.line, o.param_name))
    return orphans


# ─────────────────────────────────────────────────────────────────────────
# Allowlist (config/known_dormant.md)
# ─────────────────────────────────────────────────────────────────────────

ALLOW_LINE_RE = re.compile(
    r"^-\s*(FIELD|FUNCTION|CLASS|PARAMETER)\s*:\s*([^\s(]+)\s*\(([^)]+)\)"
)
ALLOW_FILE_RE = re.compile(r"^-\s*FILE\s*:\s*(\S+)")


def load_allowlist(path: Path = ALLOWLIST_PATH) -> tuple:
    """Returns (entries, whole_files):
      entries      -- set of (kind, name, file) tuples
      whole_files  -- set of rel-path strings whose ENTIRE contents are
                       allowlisted (every field/def/param in that file)
    """
    entries = set()
    whole_files = set()
    if not path.exists():
        return entries, whole_files
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        m_file = ALLOW_FILE_RE.match(stripped)
        if m_file:
            whole_files.add(m_file.group(1).replace("\\", "/"))
            continue
        m = ALLOW_LINE_RE.match(stripped)
        if not m:
            continue
        kind, name, file = m.group(1), m.group(2), m.group(3).strip()
        entries.add((kind, name, file.replace("\\", "/")))
    return entries, whole_files


def _is_allowed(allow: tuple, kind: str, name: str, file: str) -> bool:
    entries, whole_files = allow
    return file in whole_files or (kind, name, file) in entries


# ─────────────────────────────────────────────────────────────────────────
# Report assembly
# ─────────────────────────────────────────────────────────────────────────

def run_all():
    files = all_py_files()
    idx = build_token_index(files)
    ident_idx = build_identifier_index(files)

    schemas = find_field_schemas()
    field_orphans = check_orphan_fields(schemas, idx)

    defs = find_top_level_defs()
    def_orphans = check_orphan_defs(defs, ident_idx)

    param_orphans = check_orphan_params(src_py_files())
    # If the function/class itself has no real external caller (dead OR
    # insular), its individual unused parameters are redundant noise on top
    # of that whole-function finding -- suppress them regardless of the
    # def's own allowlist status, since the def-level entry (or a future
    # fix/removal of the def) already covers this.
    _orphaned_def_names = {(o.file, o.name) for o in def_orphans}
    param_orphans = [
        p for p in param_orphans
        if (p.file, p.func_name) not in _orphaned_def_names
    ]

    allow = load_allowlist()

    def split(items, key_fn):
        flagged, allowed = [], []
        for it in items:
            (flagged if not _is_allowed(allow, *key_fn(it)) else allowed).append(it)
        return flagged, allowed

    field_flagged, field_allowed = split(
        field_orphans, lambda o: ("FIELD", o.name, o.file))
    def_flagged, def_allowed = split(
        def_orphans, lambda o: ("CLASS" if o.kind == "class" else "FUNCTION", o.name, o.file))
    param_flagged, param_allowed = split(
        param_orphans, lambda o: ("PARAMETER", f"{o.func_name}.{o.param_name}", o.file))

    return {
        "field_orphans": field_orphans,
        "field_flagged": field_flagged,
        "field_allowed": field_allowed,
        "def_orphans": def_orphans,
        "def_flagged": def_flagged,
        "def_allowed": def_allowed,
        "param_orphans": param_orphans,
        "param_flagged": param_flagged,
        "param_allowed": param_allowed,
    }


def format_report(result: dict) -> str:
    lines = []
    lines.append("=" * 78)
    lines.append("ORPHAN DETECTION REPORT (heuristic -- see script docstring for limits)")
    lines.append("=" * 78)

    def section(title, flagged, allowed, fmt_fn):
        lines.append("")
        lines.append(f"-- {title} " + "-" * max(1, 60 - len(title)))
        if not flagged and not allowed:
            lines.append("  (none found)")
            return
        if flagged:
            lines.append(f"  {len(flagged)} FLAGGED (not on allowlist):")
            for o in flagged:
                lines.append(f"    {o.file}:{o.line}  {fmt_fn(o)}")
        else:
            lines.append("  0 flagged (not on allowlist)")
        if allowed:
            lines.append(f"  {len(allowed)} allowlisted (expected, suppressed):")
            for o in allowed:
                lines.append(f"    {o.file}:{o.line}  {fmt_fn(o)}")

    section(
        "1. ORPHANED SCHEMA FIELDS (declared, zero read/write sites found elsewhere)",
        result["field_flagged"], result["field_allowed"],
        lambda o: f'"{o.name}"  (in {o.schema_var})',
    )

    dead_flagged = [o for o in result["def_flagged"] if o.severity == "dead"]
    insular_flagged = [o for o in result["def_flagged"] if o.severity == "insular"]
    dead_allowed = [o for o in result["def_allowed"] if o.severity == "dead"]
    insular_allowed = [o for o in result["def_allowed"] if o.severity == "insular"]

    lines.append("")
    lines.append("-- 2a. DEAD FUNCTIONS / CLASSES (zero references anywhere, high confidence) " + "-" * 3)
    if not dead_flagged and not dead_allowed:
        lines.append("  (none found)")
    else:
        if dead_flagged:
            lines.append(f"  {len(dead_flagged)} FLAGGED (not on allowlist):")
            for o in dead_flagged:
                lines.append(f"    {o.file}:{o.line}  {o.kind} {o.name}()")
        else:
            lines.append("  0 flagged (not on allowlist)")
        if dead_allowed:
            lines.append(f"  {len(dead_allowed)} allowlisted (expected, suppressed):")
            for o in dead_allowed:
                lines.append(f"    {o.file}:{o.line}  {o.kind} {o.name}()")

    lines.append("")
    lines.append("-- 2b. INSULAR FUNCTIONS / CLASSES (only referenced within own file, lower "
                  "confidence) " + "-" * 3)
    lines.append("   Public (non-underscore) names normally imported elsewhere that currently "
                  "aren't. Many of")
    lines.append("   these are legitimate module-scoped helpers (e.g. a dispatcher pattern like "
                  "health_check.py's")
    lines.append("   run_all_checks() calling sibling check_*() functions) -- review "
                  "individually, don't assume dead.")
    if not insular_flagged and not insular_allowed:
        lines.append("  (none found)")
    else:
        if insular_flagged:
            lines.append(f"  {len(insular_flagged)} FLAGGED (not on allowlist):")
            for o in insular_flagged:
                lines.append(f"    {o.file}:{o.line}  {o.kind} {o.name}()")
        else:
            lines.append("  0 flagged (not on allowlist)")
        if insular_allowed:
            lines.append(f"  {len(insular_allowed)} allowlisted (expected, suppressed):")
            for o in insular_allowed:
                lines.append(f"    {o.file}:{o.line}  {o.kind} {o.name}()")

    section(
        "3. ORPHANED PARAMETERS (accepted but never referenced in the function body)",
        result["param_flagged"], result["param_allowed"],
        lambda o: f"{o.func_name}(..., {o.param_name}, ...)",
    )

    lines.append("")
    lines.append("=" * 78)
    lines.append(
        f"TOTAL NEW (unallowlisted): {len(result['field_flagged'])} fields, "
        f"{len(dead_flagged)} dead defs, {len(insular_flagged)} insular defs "
        f"(lower confidence), {len(result['param_flagged'])} params"
    )
    lines.append("=" * 78)
    return "\n".join(lines)


def main():
    result = run_all()
    if "--json" in sys.argv:
        def ser(o):
            return o.__dict__
        out = {k: [ser(o) for o in v] for k, v in result.items()}
        print(json.dumps(out, indent=2))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
