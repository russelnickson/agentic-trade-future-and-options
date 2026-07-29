"""
Static scan for hardcoded credentials in project Python sources.

Fails if any potential secret string literal is detected (API keys, tokens,
passwords, long hex/base64 blobs, or well-known credential prefixes).

Run:
    python -m unittest tests.test_security -v
    # or
    python tests/test_security.py
"""

from __future__ import annotations

import re
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

# Project root on sys.path when executed as a script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SKIP_DIR_NAMES = frozenset(
    {
        "venv",
        ".venv",
        "__pycache__",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        "site-packages",
        "dist",
        "build",
        ".tox",
    }
)

# This scanner file contains intentional pattern examples — skip it.
_SELF_NAME = Path(__file__).name

# Credential-ish LHS names (assignment or keyword argument).
_SECRET_NAME = (
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"api[_-]?secret|secret[_-]?key|private[_-]?key|auth[_-]?token|"
    r"bearer[_-]?token|password|passwd|pwd|totp[_-]?secret|"
    r"(?<![A-Za-z0-9_])secret(?![A-Za-z0-9_]))"
)

# Non-empty string literal (named quote group so backrefs stay correct when
# composed into larger patterns that already have capturing groups).
_STR_LIT = r"""(?P<q>['"])(?P<value>(?:(?!(?P=q)).)+)(?P=q)"""

_ASSIGN_RE = re.compile(
    rf"(?P<name>{_SECRET_NAME})\s*=\s*{_STR_LIT}",
    re.IGNORECASE,
)

# Long hex / base64-looking blobs assigned to any name.
_LONG_HEX_ASSIGN_RE = re.compile(
    r"""(?P<name>[A-Za-z_][\w]*)\s*=\s*(?P<q>['"])(?P<value>[0-9a-fA-F]{40,})(?P=q)"""
)
_LONG_B64_ASSIGN_RE = re.compile(
    r"""(?P<name>[A-Za-z_][\w]*)\s*=\s*(?P<q>['"])(?P<value>[A-Za-z0-9+/]{40,}={0,2})(?P=q)"""
)

# Well-known credential prefixes anywhere in a string literal.
_PREFIX_RE = re.compile(
    r"""(?P<q>['"])(?P<value>(?:
        sk_live_[A-Za-z0-9]{16,}
        |sk_test_[A-Za-z0-9]{16,}
        |rk_live_[A-Za-z0-9]{16,}
        |pk_live_[A-Za-z0-9]{16,}
        |AKIA[0-9A-Z]{16}
        |ghp_[A-Za-z0-9]{36}
        |gho_[A-Za-z0-9]{36}
        |xox[baprs]-[A-Za-z0-9-]{10,}
        |AIza[0-9A-Za-z\-_]{35}
    ))(?P=q)""",
    re.VERBOSE,
)

# Values that are documentation / placeholders, not real secrets.
_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "changeme",
        "change_me",
        "change-me",
        "placeholder",
        "your_api_key",
        "your_secret",
        "your_password",
        "your_token",
        "your_access_token",
        "api_key",
        "access_token",
        "client_secret",
        "password",
        "secret",
        "token",
        "xxx",
        "xxxx",
        "xxxxx",
        "todo",
        "fixme",
        "none",
        "null",
        "nil",
        "n/a",
        "na",
        "example",
        "sample",
        "dummy",
        "fake",
        "test",
        "testing",
        "redacted",
        "***",
        "****",
    }
)

_PLACEHOLDER_RE = re.compile(
    r"(?i)^(<[^>]+>|\$\{?\w+\}?|your[_\-\s].+|xxx+|…|\.{3}|<.*>)$"
)

# Env-var style names used as string values (e.g. os.getenv("FOO_SECRET")).
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")


@dataclass(frozen=True)
class Finding:
    path: Path
    line_no: int
    kind: str
    snippet: str

    def format(self) -> str:
        rel = self.path.relative_to(_ROOT) if self.path.is_relative_to(_ROOT) else self.path
        return f"{rel}:{self.line_no}: [{self.kind}] {self.snippet.strip()}"


def _should_skip_dir(path: Path) -> bool:
    return any(part in _SKIP_DIR_NAMES for part in path.parts)


def iter_project_python_files(root: Path = _ROOT) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if _should_skip_dir(path.relative_to(root)):
            continue
        if path.name == _SELF_NAME and path.parent.name == "tests":
            continue
        files.append(path)
    return files


def _is_benign_value(value: str) -> bool:
    cleaned = value.strip()
    if not cleaned:
        return True
    if cleaned.lower() in _PLACEHOLDER_VALUES:
        return True
    if _PLACEHOLDER_RE.match(cleaned):
        return True
    if _ENV_NAME_RE.match(cleaned):
        return True
    # Very short literals are unlikely to be real credentials.
    if len(cleaned) < 8:
        return True
    return False


def _looks_like_code_identifier(value: str) -> bool:
    """Reject values that are clearly code/docs, not opaque secrets."""
    if " " in value and not re.search(r"[0-9a-fA-F]{20,}|[A-Za-z0-9+/]{20,}", value):
        # Human sentences in comments/docstrings assigned to secret names.
        words = value.split()
        if len(words) >= 3 and sum(w.isalpha() for w in words) >= 2:
            return True
    return False


def _strip_comment(line: str) -> str:
    """Remove a trailing `#` comment when not inside a string (best-effort)."""
    in_single = False
    in_double = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and (in_single or in_double):
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i]
        i += 1
    return line


def scan_line(path: Path, line_no: int, line: str) -> list[Finding]:
    findings: list[Finding] = []
    code = _strip_comment(line)

    for match in _ASSIGN_RE.finditer(code):
        value = match.group("value")
        if _is_benign_value(value) or _looks_like_code_identifier(value):
            continue
        findings.append(
            Finding(path, line_no, "secret_assignment", match.group(0)[:120])
        )

    for match in _LONG_HEX_ASSIGN_RE.finditer(code):
        value = match.group("value")
        if _is_benign_value(value):
            continue
        # Skip obvious non-secrets (e.g. pure digit short IDs already filtered by len).
        findings.append(
            Finding(path, line_no, "long_hex_literal", match.group(0)[:120])
        )

    for match in _LONG_B64_ASSIGN_RE.finditer(code):
        value = match.group("value")
        if _is_benign_value(value):
            continue
        # Require mixed character classes so plain words don't trip the scan.
        has_upper = any(c.isupper() for c in value)
        has_lower = any(c.islower() for c in value)
        has_digit = any(c.isdigit() for c in value)
        if not ((has_upper and has_lower) or (has_digit and (has_upper or has_lower))):
            continue
        # Hex already covered; avoid double-reporting pure hex as base64.
        if re.fullmatch(r"[0-9a-fA-F]+", value):
            continue
        findings.append(
            Finding(path, line_no, "long_base64_literal", match.group(0)[:120])
        )

    for match in _PREFIX_RE.finditer(code):
        value = match.group("value")
        if _is_benign_value(value):
            continue
        findings.append(
            Finding(path, line_no, "known_secret_prefix", match.group(0)[:120])
        )

    return findings


def scan_file(path: Path) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")

    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        findings.extend(scan_line(path, line_no, line))
    return findings


def scan_repository(root: Path = _ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_project_python_files(root):
        findings.extend(scan_file(path))
    return findings


class TestNoHardcodedSecrets(unittest.TestCase):
    def test_no_hardcoded_credentials_in_python_sources(self) -> None:
        findings = scan_repository(_ROOT)
        if findings:
            report = "\n".join(f.format() for f in findings)
            self.fail(
                "Potential hardcoded credential(s) detected:\n"
                f"{report}\n"
                "Move secrets to environment variables / .env (never commit them)."
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
