"""Regression tests for install.sh's uv-managed Python FTS5 probe."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _function_body(script: str, name: str) -> str:
    marker = f"\n{name}() {{"
    start = script.find(marker)
    assert start >= 0, f"{name}() not found in scripts/install.sh"
    rest = script[start + len(marker):]
    next_func = rest.find("\n}\n\n")
    assert next_func >= 0, f"{name}() body end not found"
    return rest[:next_func]


def test_install_sh_checks_uv_python_for_sqlite_fts5():
    script = INSTALL_SH.read_text(encoding="utf-8")

    assert "_python_has_fts5() {" in script
    assert "CREATE VIRTUAL TABLE t USING fts5(x)" in script
    assert "ensure_fts5() {" in script
    assert "--reinstall" in _function_body(script, "ensure_fts5")

    check_python = _function_body(script, "check_python")
    assert check_python.count("ensure_fts5") == 2
    assert "PYTHON_PATH=\"$(\"$UV_CMD\" python find \"$PYTHON_VERSION\"" in check_python
    assert "\"$UV_CMD\" python install \"$PYTHON_VERSION\"" in check_python
