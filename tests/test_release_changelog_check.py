"""The release's changelog guard (.github/scripts/changelog-check.py).

A release is refused, before anything is tagged, unless CHANGELOG.md has a section for the version
about to be released, headed exactly `## X.Y.Z (YYYY-MM-DD)`.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "changelog-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("release_changelog_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()

GOOD = """# Changelog

## Unreleased

## 0.6.0 (2026-11-13)

- `contexto run` keeps the session under the bound after `/clear`.

## 0.5.24 (2026-11-06)

- A PDF is never estimated low.
"""


def test_a_section_for_the_version_passes():
    assert guard.problem(GOOD, "0.6.0") is None


def test_the_tag_form_is_accepted():
    assert guard.problem(GOOD, "v0.6.0") is None


def test_the_date_is_not_compared_with_today():
    text = "# Changelog\n\n## 0.6.0 (1999-01-01)\n\n- Something.\n"
    assert guard.problem(text, "0.6.0") is None


def test_a_missing_section_fails_and_says_what_to_do():
    message = guard.problem(GOOD, "0.7.0")
    assert message is not None
    assert "## 0.7.0 (YYYY-MM-DD)" in message
    assert "docs:" in message
    assert "RELEASING.md" in message


@pytest.mark.parametrize(
    "heading",
    [
        "## v0.7.0 (2026-11-13)",
        "## [0.7.0] - 2026-11-13",
        "## 0.7.0",
        "## 0.7.0 (13 November 2026)",
        "## 0.7.0 - 2026-11-13",
        "### 0.7.0 (2026-11-13)",
        "##0.7.0 (2026-11-13)",
        "## 0.7.0 (2026-11-13) first PyPI release",
    ],
)
def test_a_heading_in_another_form_fails_and_is_pointed_at(heading):
    text = f"# Changelog\n\n{heading}\n\n- Something.\n\n## 0.6.0 (2026-11-13)\n\n- Older.\n"
    message = guard.problem(text, "0.7.0")
    assert message is not None
    assert "line 3" in message
    assert heading in message


def test_another_version_that_only_contains_the_digits_is_not_a_near_miss():
    text = "# Changelog\n\n## 10.7.0 (2026-11-13)\n\n- Something.\n"
    message = guard.problem(text, "0.7.0")
    assert message is not None
    assert "line 3" not in message


def test_a_mention_in_the_text_is_not_a_heading():
    text = "# Changelog\n\n## 0.6.0 (2026-11-13)\n\n- Prepares for ## 0.7.0 (2026-12-01).\n"
    assert guard.problem(text, "0.7.0") is not None


def test_an_empty_section_fails():
    text = "# Changelog\n\n## 0.7.0 (2026-11-13)\n\n## 0.6.0 (2026-11-06)\n\n- Older.\n"
    message = guard.problem(text, "0.7.0")
    assert message is not None
    assert "empty" in message


def test_an_empty_last_section_fails():
    message = guard.problem("# Changelog\n\n## 0.7.0 (2026-11-13)\n\n", "0.7.0")
    assert message is not None
    assert "empty" in message


def test_two_sections_for_one_version_fail():
    text = "## 0.7.0 (2026-11-13)\n\n- One.\n\n## 0.7.0 (2026-11-14)\n\n- Two.\n"
    message = guard.problem(text, "0.7.0")
    assert message is not None
    assert "twice" in message or "more than once" in message


def test_the_version_must_be_the_newest_section():
    text = "# Changelog\n\n## 0.8.0 (2026-11-20)\n\n- Newer.\n\n## 0.7.0 (2026-11-13)\n\n- This one.\n"
    message = guard.problem(text, "0.7.0")
    assert message is not None
    assert "0.8.0" in message


def test_windows_line_endings_are_read():
    assert guard.problem(GOOD.replace("\n", "\r\n"), "0.6.0") is None


def test_something_that_is_not_a_version_is_refused():
    with pytest.raises(ValueError):
        guard.problem(GOOD, "0.6")
    with pytest.raises(ValueError):
        guard.problem(GOOD, "latest")


def test_unreleased_entries_are_reported_but_do_not_fail():
    text = GOOD.replace("## Unreleased\n", "## Unreleased\n\n- Not yet moved.\n")
    assert guard.problem(text, "0.6.0") is None
    assert guard.unreleased_entries(text) == ["- Not yet moved."]
    assert guard.unreleased_entries(GOOD) == []


def _run(*args, cwd):
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True)


def test_command_passes_on_a_section_and_names_it(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(GOOD, encoding="utf-8")
    result = _run("v0.6.0", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "0.6.0 (2026-11-13)" in result.stdout


def test_command_fails_without_a_section(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(GOOD, encoding="utf-8")
    result = _run("v0.7.0", cwd=tmp_path)
    assert result.returncode == 1
    assert "::error" in result.stdout
    assert "## 0.7.0 (YYYY-MM-DD)" in result.stdout


def test_command_fails_without_the_file(tmp_path):
    result = _run("v0.7.0", cwd=tmp_path)
    assert result.returncode == 1
    assert "CHANGELOG.md" in result.stdout
    assert "does not exist" in result.stdout


def test_command_reads_the_file_it_is_given(tmp_path):
    other = tmp_path / "notes.md"
    other.write_text(GOOD, encoding="utf-8")
    assert _run("0.6.0", "--file", str(other), cwd=tmp_path).returncode == 0


def test_command_refuses_a_bad_version(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(GOOD, encoding="utf-8")
    result = _run("main", cwd=tmp_path)
    assert result.returncode == 2
