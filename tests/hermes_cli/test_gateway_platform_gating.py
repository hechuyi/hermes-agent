"""Feishu runtime fork platform menu contract."""


def test_gateway_setup_platforms_are_feishu_only():
    import hermes_cli.gateway as gateway_mod

    assert [p["key"] for p in gateway_mod._all_platforms()] == ["feishu"]
