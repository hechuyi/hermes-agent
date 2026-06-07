r"""Tests for the _TOOL_MEDIA_RE regex shape used in gateway/run.py.

The runner has local MEDIA tag scanners for tool/function messages. They must
preserve Unix absolute and home-relative paths while also accepting Windows
drive-letter absolute paths such as C:\Users\test\image.png and D:/data/a.pdf.
"""

import re
import ast
from pathlib import Path

import pytest


def _tool_media_patterns_from_run_py() -> list[re.Pattern[str]]:
    run_py = Path(__file__).parents[2] / "gateway" / "run.py"
    module = ast.parse(run_py.read_text())
    patterns: list[re.Pattern[str]] = []

    for node in ast.walk(module):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "_TOOL_MEDIA_RE" for target in node.targets):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        if not node.value.args:
            continue
        pattern = ast.literal_eval(node.value.args[0])
        patterns.append(re.compile(pattern, re.IGNORECASE))

    return patterns


_TOOL_MEDIA_RE_PRE_FIX = re.compile(
    r'MEDIA:((?:/|~\/)\S+\.(?:png|jpe?g|gif|webp|'
    r'mp4|mov|avi|mkv|webm|ogg|opus|mp3|wav|m4a|'
    r'flac|epub|pdf|zip|rar|7z|docx?|xlsx?|pptx?|'
    r'txt|csv|apk|ipa))',
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    "media_tag, expected",
    [
        (r"MEDIA:C:\Users\test\image.png", r"C:\Users\test\image.png"),
        (r"MEDIA:D:\data\report.pdf", r"D:\data\report.pdf"),
        ("MEDIA:C:/Users/test/image.png", "C:/Users/test/image.png"),
        ("MEDIA:D:/data/report.pdf", "D:/data/report.pdf"),
        (r"MEDIA:C:\Users/test\image.webp", r"C:\Users/test\image.webp"),
    ],
)
def test_windows_paths_match(media_tag, expected):
    patterns = _tool_media_patterns_from_run_py()
    assert len(patterns) == 2
    for pattern in patterns:
        match = pattern.search(media_tag)
        assert match is not None
        assert match.group(1) == expected


@pytest.mark.parametrize(
    "media_tag, expected",
    [
        ("MEDIA:/tmp/output.png", "/tmp/output.png"),
        ("MEDIA:/var/log/report.pdf", "/var/log/report.pdf"),
        ("MEDIA:~/Downloads/image.jpg", "~/Downloads/image.jpg"),
    ],
)
def test_unix_paths_still_match(media_tag, expected):
    for pattern in _tool_media_patterns_from_run_py():
        match = pattern.search(media_tag)
        assert match is not None
        assert match.group(1) == expected


@pytest.mark.parametrize(
    "text",
    [
        "No MEDIA tag here",
        "MEDIA:relative/path/file.png",
        "MEDIA:file.png",
        "MEDIA:C:file.png",
        "MEDIA:/path/to/file.unknown",
        "MEDIA:/path/to/file",
        "MEDIA:",
    ],
)
def test_invalid_paths_do_not_match(text):
    for pattern in _tool_media_patterns_from_run_py():
        assert pattern.search(text) is None


@pytest.mark.parametrize(
    "media_tag",
    [
        r"MEDIA:C:\Users\test\image.png",
        "MEDIA:D:/data/report.pdf",
        r"MEDIA:C:\path\file.jpg",
    ],
)
def test_pre_fix_pattern_rejects_windows(media_tag):
    assert _TOOL_MEDIA_RE_PRE_FIX.search(media_tag) is None
