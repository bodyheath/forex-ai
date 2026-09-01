"""Regression hook for scripts/check_orphans.py.

This is a HEURISTIC static-analysis check (see that script's docstring for
its known blind spots), so it is deliberately narrow about what it fails
the suite on:

  - orphaned schema fields (FIELD)      -- asserted
  - dead functions/classes (zero refs anywhere, ANY name) -- asserted
  - orphaned parameters (PARAMETER)     -- asserted
  - "insular" functions/classes (only referenced within their own file)
    -- NOT asserted, only printed. This tier has a high legitimate-code
    rate (module-scoped helpers, dispatcher patterns like health_check.py's
    run_all_checks() calling sibling check_*() functions) and would make
    the suite fail on normal code organisation rather than real orphans.

A failure here means: something new showed up that isn't on
config/known_dormant.md. Either fix it, or -- if it's genuinely,
deliberately dormant right now -- add a documented entry to that file
explaining why and what unorphans it.
"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import check_orphans  # noqa: E402


class TestNoUnexpectedOrphans(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = check_orphans.run_all()

    def test_no_new_orphaned_fields(self):
        flagged = self.result["field_flagged"]
        if flagged:
            detail = "\n".join(
                f"  {o.file}:{o.line}  \"{o.name}\" (in {o.schema_var})"
                for o in flagged
            )
            self.fail(
                f"\n{len(flagged)} schema field(s) declared but referenced "
                f"nowhere else in the codebase -- either wire them up, or "
                f"add a `- FIELD: name (file)` entry to "
                f"config/known_dormant.md if this is deliberate:\n{detail}"
            )

    def test_no_new_dead_functions_or_classes(self):
        flagged = [o for o in self.result["def_flagged"] if o.severity == "dead"]
        if flagged:
            detail = "\n".join(
                f"  {o.file}:{o.line}  {o.kind} {o.name}()" for o in flagged
            )
            self.fail(
                f"\n{len(flagged)} function/class under src/ with zero "
                f"references anywhere -- either wire it up, remove it, or "
                f"add a `- FUNCTION:`/`- CLASS: name (file)` entry to "
                f"config/known_dormant.md if this is deliberate:\n{detail}"
            )

    def test_no_new_orphaned_parameters(self):
        flagged = self.result["param_flagged"]
        if flagged:
            detail = "\n".join(
                f"  {o.file}:{o.line}  {o.func_name}(..., {o.param_name}, ...)"
                for o in flagged
            )
            self.fail(
                f"\n{len(flagged)} function parameter(s) accepted but never "
                f"referenced in the function body -- either use them, remove "
                f"them, or add a `- PARAMETER: func.param (file)` entry to "
                f"config/known_dormant.md if this is deliberate:\n{detail}"
            )

    def test_insular_defs_informational_only(self):
        # Never fails -- printed for visibility only. See module docstring.
        flagged = [o for o in self.result["def_flagged"] if o.severity == "insular"]
        if flagged:
            print(
                f"\n[info] {len(flagged)} function/class only referenced "
                f"within its own file (not asserted -- often legitimate "
                f"module-scoped helpers). Run scripts/check_orphans.py for "
                f"the full list."
            )


if __name__ == "__main__":
    unittest.main()
