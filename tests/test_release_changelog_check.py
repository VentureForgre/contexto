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
    # Leave no __pycache__ next to the workflow's scripts.
    writes_bytecode, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = writes_bytecode
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
    assert "is not in the form" not in message


def test_a_mention_in_the_text_is_not_a_heading():
    text = "# Changelog\n\n## 0.6.0 (2026-11-13)\n\n- Prepares for ## 0.7.0 (2026-12-01).\n"
    assert guard.problem(text, "0.7.0") is not None


def test_an_empty_section_fails():
    text = "# Changelog\n\n## 0.7.0 (2026-11-13)\n\n## 0.6.0 (2026-11-06)\n\n- Older.\n"
    message = guard.problem(text, "0.7.0")
    assert message is not None
    assert "empty" in message


@pytest.mark.parametrize(
    "body",
    [
        "### Fixed\n",
        "### Added\n\n### Fixed\n",
        "Nothing yet.\n",
        "<!-- to be written -->\n",
        "<!--\nto be written\n- over several lines\n-->\n",
        "### Fixed\n\n<!-- nothing -->\n\n_Nothing yet._\n",
    ],
)
def test_a_section_with_no_entry_is_empty(body):
    text = f"# Changelog\n\n## 0.7.0 (2026-11-13)\n\n{body}\n## 0.6.0 (2026-11-06)\n\n- Older.\n"
    message = guard.problem(text, "0.7.0")
    assert message is not None
    assert "empty" in message


def test_one_entry_under_a_subheading_is_enough():
    text = "# Changelog\n\n## 0.7.0 (2026-11-13)\n\n### Fixed\n\n<!-- note -->\n- One fix.\n"
    assert guard.problem(text, "0.7.0") is None


@pytest.mark.parametrize("fence", ["```", "~~~", "````markdown"])
def test_a_heading_inside_a_code_block_is_not_a_section(fence):
    close = fence.rstrip("markdown")
    example = f"{fence}\n## 0.7.0 (2026-11-13)\n\n- An example.\n{close}\n"
    text = f"# Changelog\n\nEach heading looks like this:\n\n{example}\n## 0.6.0 (2026-11-06)\n\n- Older.\n"
    message = guard.problem(text, "0.7.0")
    assert message is not None and "no section for 0.7.0" in message
    assert guard.problem(text, "0.6.0") is None


@pytest.mark.parametrize("date", ["2026-99-99", "2026-02-30", "2026-13-01", "0000-01-01", "2026-00-10"])
def test_an_impossible_date_fails(date):
    text = f"# Changelog\n\n## 0.7.0 ({date})\n\n- Something.\n"
    message = guard.problem(text, "0.7.0")
    assert message is not None
    assert date in message and "not a date" in message and "line 3" in message


def test_an_impossible_date_on_an_older_section_does_not_block_a_release():
    text = "# Changelog\n\n## 0.7.0 (2026-11-13)\n\n- New.\n\n## 0.6.0 (2026-99-99)\n\n- Older.\n"
    assert guard.problem(text, "0.7.0") is None


@pytest.mark.parametrize("marker", ["<!-- version list -->", "<!--version list-->", "  <!-- Version List -->  "])
def test_the_semantic_release_marker_is_refused(marker):
    text = GOOD.replace("## Unreleased\n", f"## Unreleased\n\n{marker}\n")
    message = guard.problem(text, "0.6.0")
    assert message is not None
    assert "<!-- version list -->" in message and "line 5" in message
    assert "semantic-release" in message


def test_the_marker_is_refused_inside_a_code_block_too():
    # semantic-release looks for the text, not for Markdown.
    text = GOOD + "\n```\n<!-- version list -->\n```\n"
    assert "<!-- version list -->" in guard.problem(text, "0.6.0")


BUMP_GREW = "# Changelog\n\n## Unreleased\n\n## 0.6.1 (2026-11-13)\n\n- A fix.\n\n## 0.6.0 (2026-11-06)\n\n- Older.\n"


def test_a_section_written_before_the_bump_grew_is_to_be_renamed():
    message = guard.problem(BUMP_GREW, "0.7.0", last_released="v0.6.0")
    assert message is not None
    assert "no section for 0.7.0" in message
    assert 'change its heading to "## 0.7.0 (YYYY-MM-DD)"' in message
    assert "0.6.1" in message and "line 5" in message
    assert "feat:" in message
    assert "Add the section" not in message


def test_with_the_last_release_on_top_the_advice_is_to_add_a_section():
    message = guard.problem(BUMP_GREW, "0.7.0", last_released="v0.6.1")
    assert message is not None
    assert "Add the section" in message
    assert "change its heading" not in message


def test_a_changelog_behind_the_last_release_gets_the_advice_to_add_a_section():
    message = guard.problem(BUMP_GREW, "0.7.0", last_released="v0.6.5")
    assert "Add the section" in message and "change its heading" not in message


def test_without_the_last_release_both_ways_out_are_named():
    message = guard.problem(BUMP_GREW, "0.7.0")
    assert message is not None
    assert 'change its heading to "## 0.7.0 (YYYY-MM-DD)"' in message
    assert "already released" in message and "Add the section" in message


def test_the_real_changelog_passes_for_its_newest_version():
    changelog = SCRIPT.parent.parent.parent / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    newest = next(guard.HEADING.fullmatch(line).group(1) for line in text.splitlines() if guard.HEADING.fullmatch(line))
    assert guard.problem(text, newest) is None
    assert guard.unreleased_entries(text) == [] or all(entry.strip() for entry in guard.unreleased_entries(text))


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


@pytest.mark.parametrize("placeholder", ["Nothing yet.", "_Nothing yet._", "- None", "nothing"])
def test_a_placeholder_under_unreleased_is_not_an_entry(placeholder):
    text = GOOD.replace("## Unreleased\n", f"## Unreleased\n\n{placeholder}\n")
    assert guard.unreleased_entries(text) == []


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


def test_command_passes_with_a_warning_when_unreleased_has_entries(tmp_path):
    text = GOOD.replace("## Unreleased\n", "## Unreleased\n\n- Not yet moved.\n- Another.\n")
    (tmp_path / "CHANGELOG.md").write_text(text, encoding="utf-8")
    result = _run("v0.6.0", cwd=tmp_path)
    assert result.returncode == 0, result.stdout
    assert "::warning::" in result.stdout and "2 line(s)" in result.stdout
    assert "::error" not in result.stdout


def test_command_has_no_warning_for_a_placeholder_under_unreleased(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(GOOD.replace("## Unreleased\n", "## Unreleased\n\nNothing yet.\n"), "utf-8")
    result = _run("v0.6.0", cwd=tmp_path)
    assert result.returncode == 0
    assert "::warning::" not in result.stdout


def test_command_takes_the_last_released_tag(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(BUMP_GREW, encoding="utf-8")
    renamed = _run("v0.7.0", "--last-released", "v0.6.0", cwd=tmp_path)
    assert renamed.returncode == 1
    assert "change its heading" in renamed.stdout
    added = _run("v0.7.0", "--last-released", "v0.6.1", cwd=tmp_path)
    assert added.returncode == 1
    assert "change its heading" not in added.stdout and "Add the section" in added.stdout
    assert _run("v0.7.0", "--last-released", "", cwd=tmp_path).returncode == 1
    assert _run("v0.7.0", "--last-released", "main", cwd=tmp_path).returncode == 2


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


# --- code fences, as CommonMark reads them ------------------------------------------------------------


@pytest.mark.parametrize("line", ["```contexto``` is the command.", "````a ` b", "```py`"])
def test_a_backtick_line_with_another_backtick_after_the_fence_opens_no_code_block(line):
    # CommonMark: the info string of a backtick fence may not contain a backtick.
    text = GOOD.replace("## Unreleased\n", f"## Unreleased\n\n{line}\n")
    assert guard.problem(text, "0.6.0") is None


def test_a_tilde_fence_may_have_backticks_in_its_info_string():
    text = GOOD.replace("## Unreleased\n", "## Unreleased\n\n~~~ `x`\n")
    message = guard.problem(text, "0.6.0")
    assert message is not None and "never closed" in message


def test_a_heading_inside_a_code_block_that_is_never_closed_is_pointed_at():
    text = GOOD.replace("## Unreleased\n", "## Unreleased\n\n```\nAn example left open.\n")
    message = guard.problem(text, "0.6.0")
    assert message is not None
    assert "line 8" in message and "line 5" in message and "never closed" in message
    assert "Add the section" not in message and "no section" not in message


def test_a_closed_code_block_holding_the_heading_is_still_an_example():
    text = "# Changelog\n\n```\n## 0.7.0 (2026-11-13)\n\n- An example.\n```\n\n## 0.6.0 (2026-11-06)\n\n- Older.\n"
    message = guard.problem(text, "0.7.0")
    assert message is not None and "no section for 0.7.0" in message and "never closed" not in message


@pytest.mark.parametrize(
    ("opening", "not_closing", "closing"),
    [("````", "```", "````"), ("```", "~~~", "```"), ("~~~", "```", "~~~~"), ("```", "```python", "```")],
)
def test_a_code_block_is_closed_only_by_a_matching_fence(opening, not_closing, closing):
    block = f"{opening}\n{not_closing}\n## 0.7.0 (2026-11-13)\n\n- Inside the block.\n{closing}\n"
    text = f"# Changelog\n\n{block}\n## 0.6.0 (2026-11-06)\n\n- Older.\n"
    message = guard.problem(text, "0.7.0")
    assert message is not None and "no section for 0.7.0" in message
    assert guard.problem(text, "0.6.0") is None


# --- versions compared as numbers ---------------------------------------------------------------------


def test_versions_are_compared_as_numbers():
    text = "# Changelog\n\n## 0.10.0 (2026-11-13)\n\n- A fix.\n\n## 0.9.0 (2026-11-06)\n\n- Older.\n"
    # 0.10.0 is above the last release 0.9.0, so it was written for this release.
    message = guard.problem(text, "0.11.0", last_released="v0.9.0")
    assert 'change its heading to "## 0.11.0' in message and "Add the section" not in message
    # 0.10.0 is the last release: a new section is needed.
    message = guard.problem(text, "0.11.0", last_released="v0.10.0")
    assert "Add the section" in message and "change its heading" not in message


def test_a_version_is_not_a_prefix_of_another():
    text = "# Changelog\n\n## 0.10.0 (2026-11-13)\n\n- Something.\n"
    message = guard.problem(text, "0.1.0")
    assert message is not None and "no section for 0.1.0" in message
    assert "is not in the form" not in message
    assert guard.problem("# Changelog\n\n## 0.1.0 (2026-11-13)\n\n- Something.\n", "0.10.0") is not None


# --- why the heading must change ----------------------------------------------------------------------


def test_the_reason_a_feat_merged_is_given_only_when_the_version_grew():
    message = guard.problem(BUMP_GREW, "0.7.0", last_released="v0.6.0")
    assert "grew because a feat: merged" in message


@pytest.mark.parametrize(("section", "computed"), [("0.6.0", "0.5.25"), ("0.7.0", "0.6.0")])
def test_a_computed_version_below_the_section_says_so(section, computed):
    text = f"# Changelog\n\n## {section} (2026-11-13)\n\n- Something.\n\n## 0.5.24 (2026-11-06)\n\n- Older.\n"
    for last in ("v0.5.24", None):
        message = guard.problem(text, computed, last_released=last)
        assert f'change its heading to "## {computed} (YYYY-MM-DD)"' in message
        assert "grew" not in message and "feat:" not in message
        assert f"{computed}, is lower than the section written, {section}" in message
