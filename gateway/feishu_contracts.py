"""Pure Feishu contract primitives.

The helpers in this module intentionally handle only sanitized contract
material. Raw Feishu identifiers, message bodies, document content, file paths,
tokens, and secrets must be represented by stable hashes or typed metadata
before they reach these contracts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any


class FeishuContractError(ValueError):
    def __init__(
        self,
        reason: str,
        *,
        failure_class: str = "invalid_feishu_contract",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.failure_class = failure_class


_SENSITIVE_KEY_MARKERS = frozenset(
    {
        "token",
        "secret",
        "privatekey",
        "rawmessage",
        "documentcontent",
        "filepath",
        "openid",
        "userid",
        "unionid",
    }
)
_ALLOWED_SENSITIVE_METADATA_KEYS = frozenset({"tokenclass"})
_NORMALIZED_KEY_CHARS_RE = re.compile(r"[^a-z0-9]+")


def canonical_contract_json(value: Any) -> str:
    """Return deterministic JSON for sanitized Feishu contract material."""

    normalized = _canonical_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def feishu_contract_hash(
    value: Any,
    *,
    domain: str,
    version: str,
    schema_version: int = 1,
    algorithm: str = "sha256",
) -> str:
    if algorithm != "sha256":
        raise FeishuContractError(
            "unsupported contract hash algorithm",
            failure_class="unsupported_contract_hash_algorithm",
        )
    if not isinstance(domain, str) or not domain:
        raise FeishuContractError("contract hash domain must be a non-empty string")
    if not isinstance(version, str) or not version:
        raise FeishuContractError("contract hash version must be a non-empty string")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise FeishuContractError("contract schema_version must be an integer")

    envelope = {
        "algorithm": algorithm,
        "domain": unicodedata.normalize("NFC", domain),
        "schema_version": schema_version,
        "value": value,
        "version": unicodedata.normalize("NFC", version),
    }
    canonical = canonical_contract_json(envelope)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _canonical_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(asdict(value))
    if isinstance(value, Mapping):
        return _canonical_mapping(value)
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FeishuContractError("contract numeric value must be finite")
        return value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_canonical_value(item) for item in value]
    raise FeishuContractError(
        f"unsupported contract value type: {type(value).__name__}",
    )


def _canonical_mapping(value: Mapping[Any, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise FeishuContractError("contract mapping keys must be strings")
        normalized_key = unicodedata.normalize("NFC", key)
        _reject_sensitive_raw_key(normalized_key)
        if normalized_key in normalized:
            raise FeishuContractError(
                "contract mapping has duplicate keys after Unicode normalization",
            )
        normalized[normalized_key] = _canonical_value(item)
    return normalized


def _reject_sensitive_raw_key(key: str) -> None:
    comparable = _normalized_key_for_policy(key)
    if comparable in _ALLOWED_SENSITIVE_METADATA_KEYS:
        return
    if key.lower().endswith("_hash"):
        return
    for marker in _SENSITIVE_KEY_MARKERS:
        if marker in comparable:
            raise FeishuContractError(
                f"sensitive raw field is not allowed in Feishu contracts: {key}",
                failure_class="sensitive_raw_field",
            )


def _normalized_key_for_policy(key: str) -> str:
    normalized = unicodedata.normalize("NFC", key).lower()
    return _NORMALIZED_KEY_CHARS_RE.sub("", normalized)
