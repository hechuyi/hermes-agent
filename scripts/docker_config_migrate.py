#!/usr/bin/env python3
"""Run Docker boot-time config migrations safely."""
from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

from hermes_cli.config import (
    DEFAULT_CONFIG,
    get_config_path,
    get_env_path,
    migrate_config,
)
from utils import env_var_enabled


class ConfigMigrationBlocked(RuntimeError):
    def __init__(self, failure_class: str, stage: str, detail: str):
        super().__init__(detail)
        self.failure_class = failure_class
        self.stage = stage
        self.detail = detail


def _emit_failure(exc: ConfigMigrationBlocked) -> None:
    print(
        "[config-migrate] ERROR "
        f"failure_class={exc.failure_class} "
        f"stage={exc.stage} "
        "action=abort "
        f"detail={exc.detail}",
        file=sys.stderr,
    )


def _backup_path(path: Path, stamp: str) -> Path:
    base = path.with_name(f"{path.name}.bak-{stamp}")
    if not base.exists():
        return base
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.name}.bak-{stamp}.{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not choose a backup path for {path}")


def _backup_existing(paths: Iterable[Path]) -> list[Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups: list[Path] = []
    for path in paths:
        if not path.is_file():
            continue
        dest = _backup_path(path, stamp)
        shutil.copy2(path, dest)
        backups.append(dest)
    return backups


def _coerce_config_version(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _read_raw_config(path: Path) -> dict | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ConfigMigrationBlocked("config_parse_failed", "read_config", type(exc).__name__) from exc
    if not isinstance(raw, dict):
        raise ConfigMigrationBlocked(
            "config_schema_incompatible",
            "read_config",
            f"expected mapping, got {type(raw).__name__}",
        )
    return raw


def _write_raw_config(path: Path, raw: dict) -> None:
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def main() -> int:
    if env_var_enabled("HERMES_SKIP_CONFIG_MIGRATION"):
        print("[config-migrate] HERMES_SKIP_CONFIG_MIGRATION is set; skipping config migration")
        return 0

    config_path = get_config_path()
    raw_config = _read_raw_config(config_path)

    current_ver = _coerce_config_version(raw_config.get("_config_version"))
    latest_ver = _coerce_config_version(DEFAULT_CONFIG.get("_config_version", 1)) or 1
    if current_ver >= latest_ver:
        return 0

    backups = _backup_existing((config_path, get_env_path()))
    backup_text = ", ".join(str(path) for path in backups) if backups else "none"
    print(
        f"[config-migrate] Migrating config schema {current_ver} -> {latest_ver}; "
        f"backups: {backup_text}"
    )

    # Existing migrate_config() derives the current version through
    # check_config_version(), which deep-merges DEFAULT_CONFIG in this fork.
    # Stamp legacy configs as version 0 just before migration so Docker boot
    # can drive the normal migration ladder without changing non-Docker config
    # loading semantics in this headless branch.
    if "_config_version" not in raw_config:
        raw_config["_config_version"] = 0
        _write_raw_config(config_path, raw_config)

    migrate_config(interactive=False, quiet=False)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigMigrationBlocked as exc:
        _emit_failure(exc)
        raise SystemExit(1)
    except Exception as exc:
        print(
            "[config-migrate] ERROR "
            "failure_class=config_migration_failed "
            "stage=migrate_config "
            "action=abort "
            f"detail={type(exc).__name__}",
            file=sys.stderr,
        )
        raise SystemExit(1)
