"""Tests for platform_toolsets validation."""

import pytest

from hermes_cli.toolset_validation import validate_platform_toolsets


_KNOWN = {
    "hermes-cli",
    "hermes-feishu",
    "terminal",
    "web",
}


def _is_valid(name):
    return name in _KNOWN


def test_valid_config_produces_no_warnings():
    cfg = {"cli": ["hermes-cli"], "feishu": ["hermes-feishu"]}
    assert validate_platform_toolsets(cfg, _is_valid) == []


def test_corrupt_platform_alias_warns_and_suggests_correct_name():
    warnings = validate_platform_toolsets({"cli": ["hermes"]}, _is_valid)
    unknown = [w for w in warnings if "unknown toolset 'hermes'" in w]
    assert len(unknown) == 1
    assert "did you mean 'hermes-cli'?" in unknown[0]
    assert any("zero valid toolsets" in w for w in warnings)


def test_mixed_valid_and_invalid_flags_only_invalid():
    warnings = validate_platform_toolsets({"cli": ["hermes-cli", "bogus"]}, _is_valid)
    assert len(warnings) == 1
    assert "unknown toolset 'bogus'" in warnings[0]


@pytest.mark.parametrize("value", [None, {}, [], "hermes-cli", 42])
def test_non_dict_or_empty_yields_no_warnings(value):
    assert validate_platform_toolsets(value, _is_valid) == []


def test_scalar_toolset_value_is_accepted():
    assert validate_platform_toolsets({"cli": "hermes-cli"}, _is_valid) == []


def test_non_string_entries_are_skipped_not_counted_invalid():
    assert validate_platform_toolsets({"cli": [None, 123, "hermes-cli"]}, _is_valid) == []


def test_real_validate_toolset_treats_hermes_cli_valid_and_hermes_invalid():
    from toolsets import validate_toolset

    assert validate_toolset("hermes-cli") is True
    assert validate_toolset("hermes") is False
    warnings = validate_platform_toolsets({"cli": ["hermes"]}, validate_toolset)
    assert any("did you mean 'hermes-cli'?" in w for w in warnings)
