from __future__ import annotations

from pathlib import Path


TEXT_PATHS = (
    Path("AGENTS.md"),
    Path("README.md"),
    *Path("task_management").glob("*.py"),
    *Path("tests").glob("*.py"),
)


KNOWN_GOOD_KOREAN = (
    "\uc6cc\ud06c\uc20d",  # workshop
    "\ub2f9\uc2e0",  # you
    "\ub0b4\uac00",  # I will
    "\ub2e4\uc74c\uc8fc",  # next week
    "\ud300\uc6d0",  # teammate
    "\ud68c\uc758",  # meeting
)


MOJIBAKE_MARKERS = (
    "\ufffd",  # Unicode replacement character
    "\u00ec",  # common UTF-8-as-Latin-1 Korean mojibake prefix
    "\u00eb",
    "\u00ed",
    "\u00c3",
    "\u8acb",  # common CJK artifact seen in corrupted Korean fixtures
    "\u6028",
    "\u5a9b",
    "\uf9de",
)


def test_agents_md_makes_shell_utf8_policy_non_negotiable() -> None:
    guidance = Path("AGENTS.md").read_text(encoding="utf-8")

    assert "Do not put raw Korean literals inside `shell_command` command strings" in guidance
    assert "PowerShell here-strings" in guidance
    assert "Unicode escapes" in guidance
    assert "--text-file" in guidance


def test_core_korean_fixtures_are_utf8_and_not_mojibake() -> None:
    parser_source = Path("task_management/discussion_adapter.py").read_text(encoding="utf-8")

    for keyword in KNOWN_GOOD_KOREAN:
        assert keyword in parser_source

    offenders: list[str] = []
    for path in TEXT_PATHS:
        text = path.read_text(encoding="utf-8")
        for marker in MOJIBAKE_MARKERS:
            if marker in text:
                offenders.append(f"{path}:{ascii(marker)}")

    assert offenders == []
