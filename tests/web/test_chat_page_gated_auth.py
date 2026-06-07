import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CHAT_PAGE = REPO_ROOT / "web" / "src" / "pages" / "ChatPage.tsx"


def _chat_page_source() -> str:
    return CHAT_PAGE.read_text(encoding="utf-8")


def test_gated_chat_page_without_loopback_token_does_not_show_missing_token_banner():
    source = _chat_page_source()
    banner_block = re.search(
        r"const \[banner, setBanner\].*?useState<string \| null>\(\(\) =>(?P<body>.*?)\n  \);",
        source,
        re.DOTALL,
    )
    assert banner_block is not None
    body = banner_block.group("body")

    assert "Session token unavailable" in body
    assert "!window.__HERMES_SESSION_TOKEN__" in body
    assert "!window.__HERMES_AUTH_REQUIRED__" in body


def test_gated_chat_page_without_loopback_token_still_initializes_terminal_effect():
    source = _chat_page_source()
    effect_start = source.index("const token =")
    effect_end = source.index("const tierW0", effect_start)
    early_auth_block = source[effect_start:effect_end]

    assert "const gated = !!window.__HERMES_AUTH_REQUIRED__;" in early_auth_block
    assert re.search(r"if\s*\(\s*!token\s*&&\s*!gated\s*\)", early_auth_block)
