import re

import pytest

from gateway.feishu_contracts import (
    FeishuContractError,
    canonical_contract_json,
    feishu_contract_hash,
)


_SHA256_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


def test_hash_output_uses_sha256_prefix_and_lowercase_hex():
    digest = feishu_contract_hash(
        {"kind": "message", "revision": 1},
        domain="feishu.contract.test",
        version="v1",
    )

    assert _SHA256_HASH_RE.fullmatch(digest)


def test_hash_canonical_json_sorts_mapping_keys_without_reordering_arrays():
    left = {"z": [3, 2, 1], "a": {"b": 2, "a": 1}}
    right = {"a": {"a": 1, "b": 2}, "z": [3, 2, 1]}
    reordered_array = {"a": {"a": 1, "b": 2}, "z": [1, 2, 3]}

    assert canonical_contract_json(left) == canonical_contract_json(right)
    assert feishu_contract_hash(left, domain="feishu.contract.test", version="v1") == (
        feishu_contract_hash(right, domain="feishu.contract.test", version="v1")
    )
    assert feishu_contract_hash(left, domain="feishu.contract.test", version="v1") != (
        feishu_contract_hash(
            reordered_array,
            domain="feishu.contract.test",
            version="v1",
        )
    )


def test_hash_does_not_confuse_missing_and_explicit_null():
    missing = {"object": {"id_hash": "sha256:" + "0" * 64}}
    explicit_null = {
        "object": {
            "id_hash": "sha256:" + "0" * 64,
            "tenant_hash": None,
        }
    }

    assert canonical_contract_json(missing) != canonical_contract_json(explicit_null)
    assert feishu_contract_hash(missing, domain="feishu.contract.test", version="v1") != (
        feishu_contract_hash(explicit_null, domain="feishu.contract.test", version="v1")
    )


def test_hash_normalizes_strings_to_unicode_nfc():
    composed = {"title": "Caf\u00e9"}
    decomposed = {"title": "Cafe\u0301"}

    assert canonical_contract_json(composed) == canonical_contract_json(decomposed)
    assert feishu_contract_hash(composed, domain="feishu.contract.test", version="v1") == (
        feishu_contract_hash(decomposed, domain="feishu.contract.test", version="v1")
    )


def test_hash_domain_version_schema_version_and_algorithm_separate_payloads():
    payload = {"object": {"id_hash": "sha256:" + "1" * 64}}
    baseline = feishu_contract_hash(
        payload,
        domain="feishu.contract.test",
        version="v1",
        schema_version=1,
        algorithm="sha256",
    )

    assert baseline != feishu_contract_hash(
        payload,
        domain="feishu.contract.other",
        version="v1",
        schema_version=1,
        algorithm="sha256",
    )
    assert baseline != feishu_contract_hash(
        payload,
        domain="feishu.contract.test",
        version="v2",
        schema_version=1,
        algorithm="sha256",
    )
    assert baseline != feishu_contract_hash(
        payload,
        domain="feishu.contract.test",
        version="v1",
        schema_version=2,
        algorithm="sha256",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"tenant_access_token": "raw"},
        {"nested": {"clientSecret": "raw"}},
        {"nested": {"private-key": "raw"}},
        {"nested": {"rawMessage": "raw"}},
        {"nested": {"documentContent": "raw"}},
        {"nested": {"file.path": "/tmp/raw"}},
        {"nested": {"openId": "ou_raw"}},
        {"nested": {"user-id": "u_raw"}},
        {"nested": {"union_id": "on_raw"}},
    ],
)
def test_hash_rejects_sensitive_raw_fields(payload):
    with pytest.raises(FeishuContractError, match="sensitive raw field"):
        feishu_contract_hash(
            payload,
            domain="feishu.contract.test",
            version="v1",
        )


def test_hash_allows_hash_suffixed_sensitive_references_and_token_class_metadata():
    payload = {
        "open_id_hash": "sha256:" + "2" * 64,
        "authorization": {"token_class": "user_access_token"},
        "file_path_hash": "sha256:" + "3" * 64,
    }

    assert _SHA256_HASH_RE.fullmatch(
        feishu_contract_hash(payload, domain="feishu.contract.test", version="v1")
    )
