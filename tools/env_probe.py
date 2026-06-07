"""Local Python toolchain probe for the system prompt."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE_LOCK = threading.Lock()
_CACHED_LINE: Optional[str] = None

_REMOTE_BACKENDS = frozenset({
    "docker",
    "singularity",
    "modal",
    "daytona",
    "ssh",
    "managed_modal",
})


def _run(cmd: list[str], timeout: float = 3.0) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return -1, "", "not found"
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except OSError as exc:
        return -1, "", f"oserror: {exc}"
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


def _python_version_of(binary: str) -> Optional[str]:
    if not shutil.which(binary):
        return None
    rc, out, _err = _run([
        binary,
        "-c",
        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')",
    ])
    if rc == 0 and out:
        return out
    return None


def _has_pip_module(binary: str) -> bool:
    if not shutil.which(binary):
        return False
    rc, _out, _err = _run([binary, "-m", "pip", "--version"])
    return rc == 0


def _detect_pep668(binary: str) -> bool:
    if not shutil.which(binary):
        return False
    code = (
        "import os;"
        "stdlib = os.path.dirname(os.__file__);"
        "print('yes' if os.path.exists(os.path.join(stdlib, 'EXTERNALLY-MANAGED')) else 'no')"
    )
    rc, out, _err = _run([binary, "-c", code])
    return rc == 0 and out == "yes"


def _pip_python_version() -> Optional[str]:
    if not shutil.which("pip"):
        return None
    rc, out, _err = _run(["pip", "--version"])
    if rc != 0 or "(python " not in out or not out.endswith(")"):
        return None
    return out.rsplit("(python ", 1)[1][:-1].strip() or None


def _build_probe_line() -> str:
    backend = (os.getenv("TERMINAL_ENV") or "local").strip().lower()
    if backend in _REMOTE_BACKENDS:
        return ""

    py3_ver = _python_version_of("python3")
    py_ver = _python_version_of("python")
    py3_has_pip = _has_pip_module("python3") if py3_ver else False
    pip_bound_to = _pip_python_version()
    py3_pep668 = _detect_pep668("python3") if py3_ver else False
    has_uv = shutil.which("uv") is not None
    mismatch = bool(pip_bound_to and py3_ver and not py3_ver.startswith(pip_bound_to))

    if py3_ver is not None and py3_has_pip and not mismatch and (not py3_pep668 or has_uv):
        return ""

    bits: list[str] = []
    if py3_ver:
        py3_bit = f"python3={py3_ver}"
        if not py3_has_pip:
            py3_bit += " (no pip module)"
        bits.append(py3_bit)
    else:
        bits.append("python3=missing")

    if py_ver and py_ver != py3_ver:
        bits.append(f"python={py_ver}")
    elif py3_ver and not py_ver:
        bits.append("python=missing (use python3)")

    if pip_bound_to:
        if mismatch:
            bits.append(f"pip->python{pip_bound_to} (mismatch)")
        elif not py3_has_pip:
            bits.append(f"pip->python{pip_bound_to}")
    elif not py3_has_pip:
        bits.append("pip=missing")

    if py3_pep668:
        bits.append("PEP 668=yes (use venv or uv)")
    if has_uv:
        bits.append("uv=installed")

    return "Python toolchain: " + ", ".join(bits) + "."


def get_environment_probe_line(*, force_refresh: bool = False) -> str:
    global _CACHED_LINE
    if force_refresh:
        with _CACHE_LOCK:
            _CACHED_LINE = None

    if _CACHED_LINE is not None:
        return _CACHED_LINE

    with _CACHE_LOCK:
        if _CACHED_LINE is not None:
            return _CACHED_LINE
        try:
            _CACHED_LINE = _build_probe_line()
        except Exception as exc:
            logger.debug("local environment probe failed: %s", exc)
            _CACHED_LINE = ""
        return _CACHED_LINE


def _reset_cache_for_tests() -> None:
    global _CACHED_LINE
    with _CACHE_LOCK:
        _CACHED_LINE = None
