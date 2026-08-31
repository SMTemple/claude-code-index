#!/usr/bin/env python3
"""Regression test for the wp-content shape detection added to the indexer.

The risk being tested: could looks_like_wp_content() misfire on a legitimate
non-WordPress project and silently prune real source out of the index?
"""
import os
import sys
import tempfile

# Resolve the repo root from this file's location so the test runs on any
# machine or checkout, not just the author's. This repo has a shared remote.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from code_index.parser import CodeParser, looks_like_wp_content  # noqa: E402


def build(root, spec):
    for rel, content in spec.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)


PY = "def a():\n    return 1\n"
PHP = "<?php\nfunction x() { return 1; }\n"

CASES = [
    (
        "non-WP, 2 markers (plugins+themes)",
        {"plugins/core/mod.py": PY, "themes/dark/style.py": PY, "src/main.py": PY},
        False,  # expect plugins KEPT (not pruned)
    ),
    (
        "non-WP, 3 markers, NO index.php/mu-plugins",
        {"plugins/core/mod.py": PY, "themes/dark/style.py": PY,
         "uploads/.keep": "x", "src/main.py": PY},
        False,  # must NOT prune - the false positive this corroboration fixes
    ),
    (
        "wp-content shape via mu-plugins",
        {"plugins/akismet/a.php": PHP, "themes/mychild/functions.php": PHP,
         "mu-plugins/custom/c.php": PHP, "uploads/.keep": "x"},
        True,   # expect pruned (correct)
    ),
    (
        "wp-content shape via index.php (no mu-plugins)",
        {"plugins/akismet/a.php": PHP, "themes/mychild/functions.php": PHP,
         "uploads/.keep": "x", "index.php": "<?php // Silence is golden\n"},
        True,   # expect pruned (correct)
    ),
    (
        "generic python project",
        {"src/main.py": PY, "lib/util.py": PY, "tests/test_a.py": PY},
        False,
    ),
]

print("=== unit: looks_like_wp_content ===")
unit_fails = 0
for names, files, want, why in [
    (["plugins", "themes", "mu-plugins", "uploads"], [], True,
     "mu-plugins corroborates"),
    (["plugins", "themes", "uploads"], ["index.php"], True,
     "index.php corroborates"),
    (["plugins", "themes", "uploads"], [], False,
     "3 markers but NO corroboration -> must NOT fire"),
    (["plugins", "themes", "uploads"], ["package.json"], False,
     "node-ish, no corroboration"),
    (["plugins", "themes"], ["index.php"], False, "only 2 markers"),
    (["plugins", "src", "lib"], [], False, "generic project"),
    ([], [], False, "empty"),
]:
    got = looks_like_wp_content(names, files)
    if got != want:
        unit_fails += 1
    print(f"  {'OK ' if got == want else 'FAIL'} {why:<46} -> {got} (want {want})")

print("\n=== integration: discovery on synthetic trees ===")
fails = 0
with tempfile.TemporaryDirectory() as tmp:
    for i, (desc, spec, expect_pruned) in enumerate(CASES):
        # Nest the tree one level below the walk root. Parent-scoped skips are
        # deliberately NOT applied at the walk root (so the indexer can be
        # pointed directly at a wp-content dir), so a flat layout would test
        # the is_root guard rather than the shape detection.
        root = os.path.join(tmp, f"case{i}")
        spec = {os.path.join("pull", k): v for k, v in spec.items()}
        build(root, spec)
        files = [str(f).replace("\\", "/") for f in CodeParser(root).discover_files()]
        pruned = not any("/plugins/" in f for f in files)
        # only meaningful when the case actually has a plugins dir
        has_plugins_dir = any("plugins/" in k for k in spec)
        ok = (pruned == expect_pruned) if has_plugins_dir else True
        if not ok:
            fails += 1
        print(f"  {'OK ' if ok else 'FAIL'} {desc:<44} files={len(files):<3} "
              f"plugins_pruned={pruned} (expected {expect_pruned})")
        for f in sorted(files):
            print(f"          {f.split(f'case{i}/')[-1]}")

total_fails = unit_fails + fails
print(f"\nRESULT: {'PASS' if total_fails == 0 else f'{total_fails} FAILURES'}")
# Exit non-zero on failure so this can actually gate something. Printing
# "FAILURES" while exiting 0 makes the test incapable of failing.
sys.exit(1 if total_fails else 0)
