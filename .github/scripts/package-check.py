"""The checks of .github/scripts/package-check.sh that read the built artifacts.

    package-check.py WHEEL SDIST REBUILT_WHEEL REPO

WHEEL and SDIST are what ``uv build`` made from the checkout REPO. REBUILT_WHEEL is a wheel built from
the unpacked SDIST. Needs readme-renderer with its ``md`` extra, the renderer PyPI uses for the long
description, and trove-classifiers, PyPI's list of classifiers. Exits non-zero, with the reason on
stderr, on the first check that fails.
"""

import configparser
import os
import sys
import tarfile
import zipfile
from email import message_from_bytes
from html.parser import HTMLParser
from pathlib import Path

from readme_renderer.markdown import render
from trove_classifiers import classifiers as known_classifiers
from trove_classifiers import deprecated_classifiers

NAME = "contexto-proxy"
PREFIX = "contexto_proxy-"  # of the artifact names and the dist-info directory
PACKAGE = "contexto"


def check(ok: bool, message: str) -> None:
    if not ok:
        sys.exit(f"package-check: {message}")


def wheel_files(path: str) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist() if not n.endswith("/")}


def sdist_files(path: str) -> dict[str, bytes]:
    with tarfile.open(path) as t:
        return {m.name: t.extractfile(m).read() for m in t.getmembers() if m.isfile()}


def one(files: dict[str, bytes], suffix: str, what: str) -> bytes:
    found = [n for n in files if n.endswith(suffix)]
    check(len(found) == 1, f"expected one {suffix} in the {what}, found {found}")
    return files[found[0]]


class Links(HTMLParser):
    """Collects the tags, every ``href`` and ``src``, and the visible text of a rendered description."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: set[str] = set()
        self.targets: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        self.targets += [value or "" for key, value in attrs if key in ("href", "src")]

    def handle_data(self, data):
        self.text.append(data)


def check_metadata(metadata: bytes, pkg_info: bytes) -> str:
    """The wheel's METADATA and the sdist's PKG-INFO. Returns the long description."""
    check(metadata == pkg_info, "the wheel's METADATA and the sdist's PKG-INFO differ")
    message = message_from_bytes(metadata)
    check(message["Name"] == NAME, f"Name is {message['Name']!r}, expected {NAME!r}")
    check(message["Requires-Python"] == ">=3.12", f"Requires-Python is {message['Requires-Python']!r}")
    check(message["License-Expression"] == "MIT", f"License-Expression is {message['License-Expression']!r}")
    check(message["Description-Content-Type"] == "text/markdown",
          f"Description-Content-Type is {message['Description-Content-Type']!r}")
    check(not message.get_all("Requires-Dist"), f"runtime dependencies: {message.get_all('Requires-Dist')}")
    for field in ("Author-email", "Maintainer-email"):
        check(message[field] is None, f"{field} is set; the metadata carries no email address")
    for field in ("Author", "Maintainer"):
        check(message[field] == "VentureForgre", f"{field} is {message[field]!r}")
    classifiers = message.get_all("Classifier") or []
    check(not [c for c in classifiers if c.startswith("License ::")],
          "a License classifier is combined with the SPDX license expression")
    deprecated = sorted(c for c in classifiers if c in deprecated_classifiers)
    check(not deprecated, f"deprecated classifiers, which PyPI refuses: {deprecated}")
    unknown = sorted(c for c in classifiers if c not in known_classifiers)
    check(not unknown, f"classifiers that are not in the pinned trove-classifiers: {unknown}. PyPI refuses a "
                       "classifier it does not list. If https://pypi.org/classifiers/ lists it, move the "
                       "TROVE_CLASSIFIERS pin in package-check.sh")
    check(len(classifiers) == len(set(classifiers)), f"a classifier is listed twice: {classifiers}")
    print(f"ok: Name: {message['Name']}, Version: {message['Version']}, Requires-Python: "
          f"{message['Requires-Python']}, no runtime dependencies, {len(classifiers)} classifiers PyPI knows")
    return message.get_payload()


def check_wheel(files: dict[str, bytes], version: str, repo: Path) -> None:
    dist_info = f"{PREFIX}{version}.dist-info"
    top = {n.split("/", 1)[0] for n in files}
    check(top == {PACKAGE, dist_info}, f"the wheel's top level is {sorted(top)}, expected {PACKAGE}/ and {dist_info}/")
    source = repo / "src" / PACKAGE
    wanted = {f"{PACKAGE}/{p.relative_to(source).as_posix()}" for p in source.rglob("*")
              if p.is_file() and "__pycache__" not in p.parts}
    packaged = {n for n in files if n.startswith(f"{PACKAGE}/")}
    check(packaged == wanted, f"the wheel's {PACKAGE}/ differs from src/{PACKAGE}: missing "
                              f"{sorted(wanted - packaged)}, extra {sorted(packaged - wanted)}")
    for name in sorted(wanted):
        check(files[name] == (source / name.split("/", 1)[1]).read_bytes(), f"{name} differs from the checkout")
    entry_points = configparser.ConfigParser()
    entry_points.read_string(files[f"{dist_info}/entry_points.txt"].decode())
    scripts = dict(entry_points["console_scripts"]) if entry_points.has_section("console_scripts") else {}
    check(scripts == {"contexto": "contexto.cli:main"}, f"console scripts are {scripts}")
    print(f"ok: the wheel holds the {PACKAGE} package ({len(wanted)} files) and the entry point "
          f"contexto = {scripts['contexto']}")


def check_sdist(files: dict[str, bytes], version: str, repo: Path) -> None:
    root = f"{PREFIX}{version}/"
    check(all(n.startswith(root) for n in files), f"an sdist member is outside {root}")
    for name in ("PKG-INFO", "pyproject.toml", "LICENSE", "docs/pypi-description.md", f"src/{PACKAGE}/__init__.py"):
        check(root + name in files, f"the sdist has no {name}")
    for name in ("LICENSE", "docs/pypi-description.md"):
        check(files[root + name] == (repo / name).read_bytes(), f"the sdist's {name} differs from the checkout")
    print(f"ok: the sdist holds PKG-INFO, pyproject.toml, LICENSE, docs/pypi-description.md and src/{PACKAGE}")


def check_description(description: str, repo: Path) -> None:
    source = (repo / "docs" / "pypi-description.md").read_text(encoding="utf-8")
    check(description.strip() == source.strip(), "the long description is not docs/pypi-description.md")
    html = render(description)
    check(html is not None, "readme_renderer could not render the long description")
    page = Links()
    page.feed(html)
    text = " ".join("".join(page.text).split())
    check(len(text) > 500, f"the rendered long description is empty or cut short ({len(text)} characters of text)")
    check({"h1", "h2", "pre", "a"} <= page.tags, f"the rendered long description has only {sorted(page.tags)}")
    for needed in ("uv tool install contexto-proxy", "pipx install contexto-proxy", "contexto run claude"):
        check(needed in text, f"the rendered long description lacks {needed!r}")
    # The renderer gives each heading an anchor of its own (#user-content-...); those work on PyPI.
    links = [t for t in page.targets if not t.startswith("#user-content-")]
    relative = [t for t in links if not t.startswith("https://")]
    check(not relative, f"the rendered long description has links that are dead on PyPI: {relative}")
    check(len(links) >= 5, f"the rendered long description has only {len(links)} links")
    print(f"ok: readme_renderer renders the long description: {len(html)} characters of HTML, "
          f"{len(links)} links, all absolute")


def main(wheel: str, sdist: str, rebuilt: str, repo_dir: str) -> None:
    repo = Path(repo_dir)
    in_wheel, in_sdist, in_rebuilt = wheel_files(wheel), sdist_files(sdist), wheel_files(rebuilt)
    metadata = one(in_wheel, ".dist-info/METADATA", "wheel")
    version = message_from_bytes(metadata)["Version"]
    for path, suffix in ((wheel, "-py3-none-any.whl"), (sdist, ".tar.gz")):
        check(os.path.basename(path) == f"{PREFIX}{version}{suffix}",
              f"{os.path.basename(path)} is not named {PREFIX}{version}{suffix}")
    description = check_metadata(metadata, in_sdist.get(f"{PREFIX}{version}/PKG-INFO", b""))
    check_wheel(in_wheel, version, repo)
    check_sdist(in_sdist, version, repo)
    differing = sorted(n for n in in_wheel.keys() | in_rebuilt.keys() if in_wheel.get(n) != in_rebuilt.get(n))
    check(not differing, f"a wheel built from the unpacked sdist differs from the wheel in {differing}")
    print("ok: a wheel built from the unpacked sdist has the same files and the same description")
    check_description(description, repo)


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    main(*sys.argv[1:])
