"""Regression tests for bundled runtime skills in the Feishu fork."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_yuanbao_skill_is_not_bundled_in_runtime_fork():
    """Yuanbao is not a bundled runtime skill in the Feishu fork."""
    assert not (REPO_ROOT / "skills" / "yuanbao").exists()

    setup_source = (REPO_ROOT / "setup.py").read_text(encoding="utf-8")
    assert '"skills/yuanbao"' not in setup_source
    assert "'skills/yuanbao'" not in setup_source
