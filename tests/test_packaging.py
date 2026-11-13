"""The packaging facts that are fixed: the PyPI distribution is ``contexto-proxy``, the command and the
import name stay ``contexto``, and the long description PyPI shows has no link that is dead there."""

import re
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

import contexto

REPO = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
DESCRIPTION = REPO / "docs" / "pypi-description.md"
BLOB = "https://github.com/VentureForgre/contexto/blob/main/"


def test_distribution_name_command_and_python_floor():
    project = PROJECT["project"]
    assert project["name"] == "contexto-proxy"
    assert project["scripts"] == {"contexto": "contexto.cli:main"}
    assert project["requires-python"] == ">=3.12"
    assert project["dependencies"] == []
    # A build from a checkout carries the placeholder; only the release workflow stamps a real version.
    assert project["version"] == "0.0.0+checkout"
    assert "dynamic" not in project


def test_the_build_backend_is_uv_build_and_the_module_is_contexto():
    assert PROJECT["build-system"]["build-backend"] == "uv_build"
    assert all(r.startswith("uv_build") for r in PROJECT["build-system"]["requires"])
    # The distribution name no longer names the module, so the backend is told where it is.
    assert PROJECT["tool"]["uv"]["build-backend"]["module-name"] == "contexto"
    assert (REPO / "src" / "contexto" / "__init__.py").is_file()


def test_authors_and_maintainers_name_the_organization_without_an_email():
    project = PROJECT["project"]
    assert project["authors"] == [{"name": "VentureForgre"}]
    assert project["maintainers"] == [{"name": "VentureForgre"}]


def test_classifiers_cover_the_supported_pythons_and_carry_no_license():
    classifiers = PROJECT["project"]["classifiers"]
    assert len(classifiers) == len(set(classifiers))
    pythons = sorted(c.rsplit(" :: ", 1)[1] for c in classifiers
                     if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c))
    assert pythons == ["3.12", "3.13", "3.14", "3.15"]
    assert "Topic :: Internet :: Proxy Servers" in classifiers
    # `license = "MIT"` is an SPDX expression; a License classifier may not be combined with it.
    assert PROJECT["project"]["license"] == "MIT"
    assert not [c for c in classifiers if c.startswith("License ::")]


def test_project_urls_are_absolute_and_use_labels_pypi_shows_icons_for():
    urls = PROJECT["project"]["urls"]
    assert set(urls) == {"Homepage", "Repository", "Issues", "Changelog", "Documentation", "Release Notes",
                         "Security"}
    assert all(u.startswith("https://github.com/VentureForgre/contexto") for u in urls.values())
    assert urls["Changelog"] == BLOB + "CHANGELOG.md"


def test_the_lock_file_names_the_distribution():
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    assert [p["name"] for p in lock["package"] if p.get("source") == {"editable": "."}] == ["contexto-proxy"]


def test_the_installed_version_is_read_from_the_contexto_proxy_distribution():
    assert contexto.DISTRIBUTION == "contexto-proxy"
    assert contexto.__version__ == metadata.version("contexto-proxy")


def test_readme_points_at_the_pypi_description():
    assert PROJECT["project"]["readme"] == "docs/pypi-description.md"
    assert DESCRIPTION.is_file()


def link_targets(markdown: str) -> list[str]:
    """Every link and image target in ``markdown``: inline ``[text](target)`` and ``![alt](target)``,
    reference definitions ``[label]: target``, and HTML ``href``/``src`` attributes. Fenced code blocks and
    inline code are not links and are left out."""
    prose = re.sub(r"^```.*?^```[^\n]*$", "", markdown, flags=re.M | re.S)
    prose = re.sub(r"`[^`\n]*`", "", prose)
    targets = [m.group(1).strip() for m in re.finditer(r"\]\(\s*<?([^)\s>]*)>?[^)]*\)", prose)]
    targets += [m.group(1) for m in re.finditer(r"^ {0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)", prose, flags=re.M)]
    targets += [m.group(2) for m in re.finditer(r"""\b(?:href|src)\s*=\s*(["']?)([^"'\s>]*)\1""", prose, flags=re.I)]
    return targets


@pytest.mark.parametrize(("markdown", "targets"), [
    ("See [the README](README.md) and ![shot](docs/shot.png).", ["README.md", "docs/shot.png"]),
    ("[a](#install) [b](<docs/a b.md>) [c](https://example.com/x 'title')",
     ["#install", "docs/a", "https://example.com/x"]),
    ("[docs]: docs/install.md\n[site]: <https://example.com>", ["docs/install.md", "https://example.com"]),
    ('<a href="LICENSE">x</a> <img src=shot.png>', ["LICENSE", "shot.png"]),
    ("```sh\ncat [x](y.md)\n```\nand `[x](z.md)` are code", []),
])
def test_link_targets_finds_every_kind_of_link(markdown, targets):
    assert link_targets(markdown) == targets


def test_pypi_description_has_no_relative_link_or_image():
    text = DESCRIPTION.read_text(encoding="utf-8")
    targets = link_targets(text)
    assert len(targets) >= 5, targets  # the README, verification, changelog, security policy and license
    relative = [t for t in targets if not t.startswith("https://")]
    assert not relative, f"relative links are dead on PyPI: {relative}"
    # Reference-style links need a definition; an undefined one renders as plain text.
    prose = re.sub(r"^```.*?^```[^\n]*$", "", text, flags=re.M | re.S)
    assert not re.search(r"\]\[[^\]]*\]", prose)


def test_pypi_description_says_what_the_reader_needs():
    text = DESCRIPTION.read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 80
    for needed in (
        "uv tool install contexto-proxy",
        "pipx install contexto-proxy",
        "uvx --from contexto-proxy contexto --version",
        "contexto run claude",
        "contexto run codex",
        "Python 3.12",
        "Windows is not supported",
        BLOB + "README.md",
        BLOB + "CHANGELOG.md",
        BLOB + "SECURITY.md",
        BLOB + "LICENSE",
    ):
        assert needed in text, needed
    # The docs voice: no decision ids, no email address, no emoji or other non-ASCII decoration.
    assert not re.search(r"\bD\d+\b", text)
    assert "@" not in text
    assert text.isascii()


def job_matrix(workflow: str, job: str) -> str:
    """The text of ``job``'s ``matrix:`` block in a workflow file, without comments."""
    block = re.search(rf"^  {re.escape(job)}:\n(.*?)(?=^  \S|\Z)", workflow, flags=re.M | re.S)
    assert block, job
    matrix = re.search(r"^      matrix:\n((?:        .*\n|\s*\n)+)", block.group(1), flags=re.M)
    assert matrix, job
    return "\n".join(line for line in matrix.group(1).splitlines() if line.strip() and not line.strip().startswith("#"))


def test_ci_installs_the_built_wheel_on_every_leg_the_tests_run_on():
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert job_matrix(workflow, "install") == job_matrix(workflow, "test")
    assert "3.11" not in workflow
    # The one required check waits for the new jobs too.
    needs = re.search(r"^  ci:\n.*?^    needs: \[([^\]]*)\]", workflow, flags=re.M | re.S)
    assert needs and {n.strip() for n in needs.group(1).split(",")} == {"lint", "test", "install", "package"}
    gate = workflow[workflow.index("\n  ci:\n"):]
    for job in ("lint", "test", "install", "package"):
        name = job.upper()
        assert f"{name}: ${{{{ needs.{job}.result }}}}" in gate, job
        assert re.search(rf'^\s+test "\${name}" = success$', gate, flags=re.M), job
