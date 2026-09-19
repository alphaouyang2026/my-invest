"""Every command-line entry point must resolve the names it imports.

A broken import in a script is invisible to the rest of the suite: nothing
imports these, so a rename underneath them is found only by running the command.
That is late, and on a long experiment it is expensive -- the import that
prompted this file failed after the container had already started.

Module-level imports are covered by importing the module. The ones that matter
more are deferred inside functions, which is where these scripts put Qlib so that
`--help` stays fast, and exactly where a rename hides.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SCRIPTS = sorted(path.stem for path in SCRIPTS_DIR.glob("*.py") if path.stem != "__init__")


def test_the_scripts_directory_is_not_empty() -> None:
    assert SCRIPTS


@pytest.mark.parametrize("name", SCRIPTS)
def test_a_script_imports(name: str) -> None:
    importlib.import_module(f"scripts.{name}")


def _deferred_imports(source: str):
    """Every `from app... import x` that sits inside a function body."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom) and (inner.module or "").startswith("app."):
                for alias in inner.names:
                    yield inner.module, alias.name


@pytest.mark.parametrize("name", SCRIPTS)
def test_a_script_resolves_the_imports_it_defers(name: str) -> None:
    source = (SCRIPTS_DIR / f"{name}.py").read_text(encoding="utf-8")
    unresolved = []
    for module_name, attribute in _deferred_imports(source):
        module = importlib.import_module(module_name)
        if not hasattr(module, attribute):
            unresolved.append(f"{module_name}.{attribute}")
    assert not unresolved, f"scripts/{name}.py imports names that no longer exist: {unresolved}"
