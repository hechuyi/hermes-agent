"""Regression tests for packaging metadata in pyproject.toml."""

from pathlib import Path
import re
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_optional_dependencies():
    pyproject_path = REPO_ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return project["optional-dependencies"]


def _load_package_data():
    pyproject_path = REPO_ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as handle:
        tool = tomllib.load(handle)["tool"]
    return tool["setuptools"]["package-data"]


def test_non_feishu_channel_extras_are_removed():
    optional_dependencies = _load_optional_dependencies()

    removed = {
        "discord",
        "discord-admin",
        "discord_admin",
        "telegram",
        "yuanbao",
        "slack",
        "matrix",
        "dingtalk",
        "wecom",
        "homeassistant",
        "sms",
        "whatsapp",
        "signal",
    }
    assert removed.isdisjoint(optional_dependencies)
    for extra_specs in optional_dependencies.values():
        assert not any(
            any(f"hermes-agent[{extra}]" in spec for extra in removed)
            for spec in extra_specs
        )


def test_lazy_installable_extras_excluded_from_all():
    """Policy (2026-05-12): every extra that has a `LAZY_DEPS` entry
    in `tools/lazy_deps.py` must be excluded from [all].

    The lazy-install system exists so one quarantined PyPI release
    (e.g. mistralai 2.4.6) can't break every fresh install. Putting a
    backend in BOTH [all] and LAZY_DEPS defeats that — fresh installs
    eager-install it and inherit whatever's broken upstream.

    If you're tempted to add an opt-in backend to [all] for "convenience,"
    add it to `LAZY_DEPS` instead so it installs at first use.
    """
    optional_dependencies = _load_optional_dependencies()

    # Hard-coded mirror of the extras that are in LAZY_DEPS as of
    # 2026-05-12. This list intentionally duplicates rather than
    # imports tools/lazy_deps.py so the test stays a contract — if
    # someone adds a new lazy-install backend, they have to update
    # this list AND verify [all] doesn't contain it.
    lazy_covered_extras = {
        "anthropic", "bedrock",
        "exa", "firecrawl", "parallel-web",
        "fal",
        "edge-tts", "tts-premium",
        "voice",  # faster-whisper / sounddevice / numpy
        "modal", "daytona",
        "messaging", "feishu",
        "honcho", "hindsight",
        "mistral",  # mistralai — Voxtral STT/TTS, lazy-installed (stt.mistral / tts.mistral)
    }
    all_extra_specs = optional_dependencies["all"]
    for extra in lazy_covered_extras:
        offending = [
            spec for spec in all_extra_specs
            if f"hermes-agent[{extra}]" in spec
        ]
        assert not offending, (
            f"[{extra}] is in [all] but also in LAZY_DEPS. "
            f"Remove it from [all] in pyproject.toml — it lazy-installs "
            f"at first use. Found in [all]: {offending}"
        )


def _exact_pins(specs):
    pins = {}
    for spec in specs:
        requirement = spec.split(";", 1)[0].strip()
        if "==" not in requirement:
            continue
        package, version = requirement.split("==", 1)
        package = package.split("[", 1)[0].lower().replace("_", "-")
        pins[package] = version
    return pins


def test_pyproject_pins_match_lazy_deps_pins():
    """Generalize #31817 to the whole pin surface, not just aiohttp.

    Any package that is exact-pinned in BOTH a pyproject extra and a
    `tools/lazy_deps.py` LAZY_DEPS entry must use the SAME version in both
    places. When they drift, `hermes update` resolves the pyproject extra
    pin and downgrades the package to the older version, reopening whatever
    the lazy pin fixed (the aiohttp #31817 case, and the anthropic
    CVE-2026-34450/34452 case found alongside it) — only for the lazy
    refresh to re-upgrade it on next feature use. The lazy pin is the
    security-current source of truth; extras must track it.
    """
    from tools.lazy_deps import LAZY_DEPS

    optional_dependencies = _load_optional_dependencies()

    # package -> version, as pinned across all pyproject extras. If an
    # extra pins a package at a different version than another extra, that
    # is itself a bug (caught below); here we just collect the set.
    pyproject_pins: dict[str, set[str]] = {}
    for specs in optional_dependencies.values():
        for package, version in _exact_pins(specs).items():
            pyproject_pins.setdefault(package, set()).add(version)

    # package -> version, as pinned across all LAZY_DEPS entries.
    lazy_pins: dict[str, set[str]] = {}
    for specs in LAZY_DEPS.values():
        if isinstance(specs, str):
            specs = (specs,)
        for package, version in _exact_pins(specs).items():
            lazy_pins.setdefault(package, set()).add(version)

    shared = sorted(set(pyproject_pins) & set(lazy_pins))
    assert shared, "expected at least one package pinned in both pyproject and LAZY_DEPS"

    drift = {
        package: {
            "pyproject": sorted(pyproject_pins[package]),
            "lazy_deps": sorted(lazy_pins[package]),
        }
        for package in shared
        if pyproject_pins[package] != lazy_pins[package]
    }
    assert not drift, (
        "pyproject extras pins must match tools/lazy_deps.py LAZY_DEPS pins "
        "for every shared package — otherwise `hermes update` downgrades the "
        "package below the security-current lazy pin (see #31817). Drift: "
        f"{drift}"
    )


def test_dev_extra_excluded_from_all():
    """End-user installs should not pull test/lint/debug tooling."""
    optional_dependencies = _load_optional_dependencies()

    assert "dev" in optional_dependencies
    assert not any(
        spec == "hermes-agent[dev]"
        for spec in optional_dependencies["all"]
    )


def test_messaging_extra_includes_feishu_runtime_deps():
    optional_dependencies = _load_optional_dependencies()

    messaging_extra = optional_dependencies["messaging"]
    feishu_extra = optional_dependencies["feishu"]
    assert messaging_extra == feishu_extra
    assert any(dep.startswith("lark-oapi") for dep in messaging_extra)
    assert any(dep.startswith("qrcode") for dep in messaging_extra)


def test_feishu_extra_includes_qrcode_for_qr_login():
    """Feishu's QR login flow (gateway/platforms/feishu.py) needs the
    qrcode package."""
    optional_dependencies = _load_optional_dependencies()

    feishu_extra = optional_dependencies["feishu"]
    assert any(dep.startswith("qrcode") for dep in feishu_extra)


def test_dashboard_extra_is_removed_from_feishu_runtime_fork():
    """The Feishu runtime fork must not publish a dashboard backend extra."""
    optional_dependencies = _load_optional_dependencies()

    assert "web" not in optional_dependencies
    assert not any(
        spec == "hermes-agent[web]"
        for spec in optional_dependencies["all"]
    )
    assert not any(
        spec == "hermes-agent[web]"
        for spec in optional_dependencies["termux-all"]
    )


def test_dashboard_plugin_manifests_are_not_packaged():
    """Dashboard plugin discovery metadata is not part of the Feishu wheel."""
    package_data = _load_package_data()
    plugin_data = package_data["plugins"]

    assert "*/dashboard/manifest.json" not in plugin_data
    assert "*/dashboard/dist/*" not in plugin_data
    assert "*/dashboard/dist/**/*" not in plugin_data


def test_dashboard_runtime_files_are_removed_from_feishu_runtime_fork():
    """The Feishu fork must not ship the dashboard runtime or auth backend."""
    forbidden = [
        "hermes_cli/web_server.py",
        "hermes_cli/pty_bridge.py",
        "hermes_cli/dashboard_auth",
        "plugins/dashboard_auth/nous",
        "plugins/example-dashboard",
        "plugins/hermes-achievements",
        "plugins/kanban/dashboard/manifest.json",
        "plugins/kanban/dashboard/plugin_api.py",
        "plugins/kanban/systemd/hermes-kanban-dispatcher.service",
        "docker/s6-rc.d/dashboard",
        "docker/s6-rc.d/user/contents.d/dashboard",
        "docs/hermes-kanban-v1-spec.pdf",
        "infographic/kanban-db-corruption-defense/infographic.png",
        "tests/hermes_cli/test_dashboard_profiles_nav_label.py",
    ]

    for relpath in forbidden:
        assert not (REPO_ROOT / relpath).exists(), relpath


def test_dashboard_cli_entrypoint_is_removed_from_feishu_runtime_fork():
    """The Feishu fork must not retain even a disabled dashboard command."""
    main_source = (REPO_ROOT / "hermes_cli" / "main.py").read_text(encoding="utf-8")
    forbidden_snippets = [
        "def cmd_dashboard(",
        'add_parser(\n        "dashboard"',
        '"dashboard", "debug"',
        '"profile",\n        "dashboard"',
        "Dashboard is disabled in this Feishu runtime fork",
    ]
    for snippet in forbidden_snippets:
        assert snippet not in main_source, snippet


def test_non_feishu_platform_plugins_are_removed_from_runtime_fork():
    """The Feishu runtime fork must not ship extra messaging platform plugins."""
    assert not (REPO_ROOT / "plugins" / "platforms").exists()


def test_gateway_adapter_factory_is_feishu_api_only():
    """The Feishu fork must not construct non-Feishu messaging adapters."""
    source = (REPO_ROOT / "gateway" / "run.py").read_text(encoding="utf-8")
    start = source.index("    def _create_adapter(")
    end = source.index("    def _is_user_authorized(", start)
    factory_source = source[start:end]

    assert "gateway.platform_registry" not in factory_source
    assert "platform_registry" not in factory_source

    platform_refs = set(re.findall(r"\bPlatform\.([A-Z_]+)\b", factory_source))
    assert platform_refs == {"FEISHU", "API_SERVER"}

    adapter_imports = set(re.findall(r"gateway\.platforms\.([a-z_]+)", factory_source))
    assert adapter_imports == {"feishu", "api_server"}


def test_cli_platform_registry_is_static_for_feishu_fork():
    """Platform menus must not re-import channel plugins in this fork."""
    from hermes_cli.platforms import PLATFORMS
    from runtime_profile import CLI_TOOL_PLATFORMS

    assert list(PLATFORMS) == list(CLI_TOOL_PLATFORMS)
    assert {
        key: info.default_toolset
        for key, info in PLATFORMS.items()
    } == {
        key: info.default_toolset
        for key, info in CLI_TOOL_PLATFORMS.items()
    }

    source = (REPO_ROOT / "hermes_cli" / "platforms.py").read_text(encoding="utf-8")

    assert "gateway.platform_registry" not in source
    assert "plugin_entries" not in source
    assert '"discord"' not in source
    assert '"telegram"' not in source
    assert '"yuanbao"' not in source


def test_gateway_runtime_allowed_platforms_follow_profile():
    """Gateway config pruning must use the same positive runtime profile."""
    from gateway.config import _ALLOWED_RUNTIME_PLATFORMS, Platform
    from runtime_profile import GATEWAY_RUNTIME_PLATFORM_VALUES

    assert _ALLOWED_RUNTIME_PLATFORMS == {
        Platform(value) for value in GATEWAY_RUNTIME_PLATFORM_VALUES
    }


def test_toolsets_do_not_autogenerate_plugin_platform_aliases():
    """Toolset resolution must not synthesize hermes-<platform> aliases."""
    source = (REPO_ROOT / "toolsets.py").read_text(encoding="utf-8")

    assert "gateway.platform_registry" not in source
    assert "platform_registry.is_registered" not in source


def test_legacy_messaging_tool_identifiers_are_not_exposed():
    """Core tool metadata must not expose removed Discord/Yuanbao tools."""
    scanned_files = [
        "model_tools.py",
        "acp_adapter/tools.py",
        "toolsets.py",
        "tools/registry.py",
    ]
    forbidden_snippets = [
        "discord",
        "discord_admin",
        "yuanbao",
        "yb_",
    ]

    offenders: list[str] = []
    for relpath in scanned_files:
        source = (REPO_ROOT / relpath).read_text(encoding="utf-8").lower()
        for snippet in forbidden_snippets:
            if snippet in source:
                offenders.append(f"{relpath}: {snippet}")

    assert not offenders


def test_removed_self_registering_messaging_tool_modules_are_absent():
    """Deleted platform tools must not be rediscovered as built-in tools."""
    removed_modules = [
        "tools/discord_tool.py",
        "tools/yuanbao_tools.py",
    ]
    for relpath in removed_modules:
        assert not (REPO_ROOT / relpath).exists(), relpath

    from tools.registry import discover_builtin_tools

    discovered = set(discover_builtin_tools(REPO_ROOT / "tools"))
    assert "tools.discord_tool" not in discovered
    assert "tools.yuanbao_tools" not in discovered


def test_hermes_feishu_runtime_tool_names_do_not_expose_legacy_messaging_tools():
    """The Feishu runtime toolset/model schemas must not expose legacy tools."""
    forbidden_names = {
        "discord",
        "discord_admin",
        "yuanbao",
        "yb_chat",
        "yb_search",
        "yb_open",
        "yb_login",
        "yb_post",
    }

    from model_tools import get_tool_definitions
    from toolsets import resolve_toolset

    resolved_toolset_names = set(resolve_toolset("hermes-feishu"))
    model_tool_names = {
        definition["function"]["name"]
        for definition in get_tool_definitions(
            enabled_toolsets=["hermes-feishu"],
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
    }

    assert forbidden_names.isdisjoint(resolved_toolset_names)
    assert forbidden_names.isdisjoint(model_tool_names)
    assert not any(name.startswith("yb_") for name in resolved_toolset_names)
    assert not any(name.startswith("yb_") for name in model_tool_names)


def test_removed_platform_plugin_surfaces_leave_no_runtime_references():
    """Deleted bundled platform plugins must not leave importable runtime hooks."""
    ignored_roots = {
        ".git",
        ".mypy_cache",
        ".plans",
        ".pytest-cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "docs/plans",
        "docs/superpowers/plans",
        "hermes_agent.egg-info",
    }
    ignored_suffixes = {
        ".pyc",
        ".pyo",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".pdf",
        ".sqlite",
        ".db",
    }
    forbidden_snippets = [
        "plugins/platforms",
        "plugins.platforms",
        "plugins/teams_pipeline",
        "plugins.teams_pipeline",
        "teams_pipeline",
    ]
    offenders: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel == "tests/test_project_metadata.py":
            continue
        if any(rel == root or rel.startswith(f"{root}/") for root in ignored_roots):
            continue
        if path.suffix.lower() in ignored_suffixes:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for snippet in forbidden_snippets:
            if snippet in source:
                offenders.append(f"{rel}: {snippet}")

    assert not offenders


def test_node_tui_launcher_is_removed_from_feishu_runtime_fork():
    """The Feishu fork must not retain the Node/Ink TUI launch surface."""
    forbidden_paths = [
        "ui-tui",
        "tui_gateway",
        "hermes_cli/tui_dist",
    ]
    for relpath in forbidden_paths:
        assert not (REPO_ROOT / relpath).exists(), relpath

    main_source = (REPO_ROOT / "hermes_cli" / "main.py").read_text(encoding="utf-8")
    parser_source = (REPO_ROOT / "hermes_cli" / "_parser.py").read_text(encoding="utf-8")
    forbidden_snippets = [
        "def _launch_tui(",
        "def _make_tui_argv(",
        "def _ensure_tui_node(",
        "def _tui_need_npm_install(",
        "def _suppress_mouse_residue_early(",
        "def _try_termux_fast_tui_launch(",
        "HERMES_TUI_NO_EARLY_DISABLE",
        "HERMES_TUI",
        "ui-tui",
        "tui_dist",
        '"--tui"',
        "tui_dev",
        "TUI is not available in this Feishu runtime fork",
    ]
    for snippet in forbidden_snippets:
        assert snippet not in main_source, snippet
        assert snippet not in parser_source, snippet


def test_computer_use_desktop_surface_is_removed_from_feishu_runtime_fork():
    """The Feishu fork must not retain the desktop/computer-use surface."""
    forbidden_paths = [
        "tools/computer_use",
        "tools/computer_use_tool.py",
        "skills/apple/macos-computer-use",
        "tests/tools/test_computer_use.py",
        "tests/tools/test_computer_use_capture_routing.py",
        "tests/tools/test_computer_use_vision_routing.py",
        "tests/hermes_cli/test_install_cua_driver.py",
    ]
    for relpath in forbidden_paths:
        assert not (REPO_ROOT / relpath).exists(), relpath

    main_source = (REPO_ROOT / "hermes_cli" / "main.py").read_text(encoding="utf-8")
    tools_config_source = (REPO_ROOT / "hermes_cli" / "tools_config.py").read_text(encoding="utf-8")
    prompt_builder_source = (REPO_ROOT / "agent" / "prompt_builder.py").read_text(encoding="utf-8")
    system_prompt_source = (REPO_ROOT / "agent" / "system_prompt.py").read_text(encoding="utf-8")

    forbidden_snippets = [
        '"computer-use"',
        "computer_use",
        "cua-driver",
        "cua_driver",
        "Computer Use",
        "COMPUTER_USE_GUIDANCE",
    ]
    for snippet in forbidden_snippets:
        assert snippet not in main_source, snippet
        assert snippet not in tools_config_source, snippet
        assert snippet not in prompt_builder_source, snippet
        assert snippet not in system_prompt_source, snippet
