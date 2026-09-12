"""
Regression check for the intentional code duplication across
weekly_report_generator.py / monthly_report_generator.py / ytd_report_generator.py.

CLAUDE.md flags these three as "near-duplicates ... with only the
period-slicing and baseline-averaging logic differing" and warns: "When
fixing a bug or adding a brand/country in one, check whether the same change
is needed in the other two -- there's no shared module they import from."
This script turns that warning into a check instead of relying on memory.

Rule enforced:
  - weekly and monthly must be identical (after normalizing away purely
    cosmetic parameter-name differences) for every shared helper/constant
    checked here. Nothing distinguishes weekly from monthly at this level,
    so ANY difference is unexpected drift.
  - ytd is allowed exactly one documented exception: the brand_1.com
    YTD-only handling (see config/business_rules.md -- excluded from
    weekly/monthly, kept in YTD). YTD_ALLOWED_DIFFS below is a fixed
    allowlist of the specific line content that exception is permitted to
    introduce. Any ytd difference from weekly NOT covered by that allowlist
    is flagged as unexpected drift, not silently accepted.

This is a manual/dev-time check, not wired into run_report.py -- run it
after editing any of the three generators:
    python -m src.utils.generator_consistency_check
Exits non-zero (and prints every violation) if unexpected drift is found.
"""
import ast
import difflib
import sys

GENERATORS = {
    'weekly': 'src/generators/weekly_report_generator.py',
    'monthly': 'src/generators/monthly_report_generator.py',
    'ytd': 'src/generators/ytd_report_generator.py',
}

FUNCS_TO_CHECK = [
    'get_country', 'get_brand_color', 'calc_var',
    'format_var', 'format_budget_cell', 'get_extreme_outliers',
]

CONSTANTS_TO_CHECK = ['COUNTRY_ORDER', 'BRAND_ORDER']

# Known, accepted content that ytd is allowed to add/change relative to
# weekly. A changed line is only accepted if it CONTAINS one of these
# substrings -- anything else is unexpected drift.
YTD_ALLOWED_DIFFS = {
    'get_country': ["brand_1.com"],
    'get_brand_color': ["brand_1.com"],
    'get_extreme_outliers': ["min_impact_eur"],
    'calc_var': [],
    'format_var': [],
    'format_budget_cell': [],
}

# Elements ytd's list constants are allowed to contain beyond weekly's list.
YTD_ALLOWED_EXTRA_ELEMENTS = {
    'COUNTRY_ORDER': [],
    'BRAND_ORDER': ['brand_1.com'],
}


def _load_ast(path):
    with open(path, encoding='utf-8') as f:
        return ast.parse(f.read())


def _get_function_node(tree, fname):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fname:
            return node
    return None


def _normalize_function_source(node):
    """Canonical source for a function, with its first parameter (and all
    Name references to it) renamed to a fixed placeholder so a purely
    cosmetic parameter-name difference (e.g. df_cw vs df_cm) doesn't
    register as drift."""
    node = ast.parse(ast.unparse(node)).body[0]  # deep-copy via round-trip
    if node.args.args:
        real_name = node.args.args[0].arg
        node.args.args[0].arg = '_ARG0'

        class Renamer(ast.NodeTransformer):
            def visit_Name(self, n):
                if n.id == real_name:
                    n.id = '_ARG0'
                return n

        node = Renamer().visit(node)
    node.name = '_FUNC'
    return ast.unparse(node)


def _get_constant_value(tree, cname):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == cname for t in node.targets):
            return ast.literal_eval(node.value)
    return None


def _changed_blocks(a_src, b_src):
    """Grouped by difflib opcode, not a flat unified diff -- so a single-line
    replacement (e.g. "if X:" -> "if X or Y:") is evaluated as one block
    against the allowlist, instead of splitting into a removed old line and
    an added new line that get checked independently (the removed line alone
    never contains the new substring, which produced false positives)."""
    a_lines, b_lines = a_src.splitlines(), b_src.splitlines()
    sm = difflib.SequenceMatcher(a=a_lines, b=b_lines, autojunk=False)
    blocks = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != 'equal':
            blocks.append(a_lines[i1:i2] + b_lines[j1:j2])
    return blocks


def check_functions(trees):
    violations = []
    for fname in FUNCS_TO_CHECK:
        nodes = {gen: _get_function_node(tree, fname) for gen, tree in trees.items()}
        missing = [gen for gen, n in nodes.items() if n is None]
        if missing:
            violations.append(f"{fname}: missing from {missing}")
            continue

        normalized = {gen: _normalize_function_source(n) for gen, n in nodes.items()}

        wm_blocks = _changed_blocks(normalized['weekly'], normalized['monthly'])
        if wm_blocks:
            violations.append(
                f"{fname}: weekly and monthly differ (no known exception applies here):\n    "
                + "\n    ".join(l for block in wm_blocks for l in block)
            )

        wy_blocks = _changed_blocks(normalized['weekly'], normalized['ytd'])
        allowed = YTD_ALLOWED_DIFFS.get(fname, [])
        unexpected_blocks = [
            block for block in wy_blocks
            if not any(sub in line for line in block for sub in allowed)
        ]
        if unexpected_blocks:
            violations.append(
                f"{fname}: ytd differs from weekly beyond the allowed brand_1.com exception:\n    "
                + "\n    ".join(l for block in unexpected_blocks for l in block)
            )
    return violations


def check_constants(trees):
    violations = []
    for cname in CONSTANTS_TO_CHECK:
        values = {gen: _get_constant_value(tree, cname) for gen, tree in trees.items()}
        missing = [gen for gen, v in values.items() if v is None]
        if missing:
            violations.append(f"{cname}: missing from {missing}")
            continue

        if values['weekly'] != values['monthly']:
            violations.append(
                f"{cname}: weekly and monthly differ (no known exception applies here):\n"
                f"    weekly:  {values['weekly']}\n    monthly: {values['monthly']}"
            )

        allowed_extra = YTD_ALLOWED_EXTRA_ELEMENTS.get(cname, [])
        ytd_without_extra = [v for v in values['ytd'] if v not in allowed_extra]
        if ytd_without_extra != values['weekly']:
            violations.append(
                f"{cname}: ytd differs from weekly beyond the allowed extra elements {allowed_extra}:\n"
                f"    weekly: {values['weekly']}\n    ytd:    {values['ytd']}"
            )
    return violations


def main():
    trees = {gen: _load_ast(path) for gen, path in GENERATORS.items()}

    violations = check_functions(trees) + check_constants(trees)

    if violations:
        print(f"Generator consistency check FAILED -- {len(violations)} unexpected drift item(s):\n")
        for v in violations:
            print(f"- {v}\n")
        sys.exit(1)

    print(
        f"Generator consistency check OK -- {len(FUNCS_TO_CHECK)} shared functions and "
        f"{len(CONSTANTS_TO_CHECK)} shared constants match across weekly/monthly/ytd "
        f"(modulo the documented brand_1.com YTD exception)."
    )


if __name__ == '__main__':
    main()
