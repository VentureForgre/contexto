#!/usr/bin/env python3
"""Checks for the Release workflow's pypi job. Standard library only. It never sends a file anywhere.

    python3 -I .github/scripts/pypi-publish.py verify  --dist DIR --project NAME --tag vX.Y.Z
    python3 -I .github/scripts/pypi-publish.py plan    --dist DIR --project NAME --tag vX.Y.Z --output FILE
    python3 -I .github/scripts/pypi-publish.py confirm --dist DIR --project NAME --tag vX.Y.Z

DIR holds what `gh release download` fetched from the tag's GitHub Release: one wheel, one sdist
and SHA256SUMS.

verify   Refuse the files unless there is exactly one wheel and one sdist, SHA256SUMS lists exactly
         those two with matching digests, and the metadata of both says the project's name and the
         tag's version. A build from before the distribution was renamed, or a build from a
         checkout, stops here.
plan     Ask the index what it holds for the version and write the paths that may be uploaded to
         FILE, one per line (and `upload_count` to GITHUB_OUTPUT):
           404                              both files
           200, same sha256 for every file  the files the index does not have yet, possibly none
           200, a file with another sha256  fail: a version on PyPI cannot be replaced
           anything else                    fail without a plan
confirm  After the upload, wait until the index lists both files with this release's digests.

Every subcommand runs verify first. The upload itself is `uv publish` in the workflow.
"""

import argparse
import email.parser
import hashlib
import json
import os
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

RELEASE_TAG = re.compile(r"v(\d+\.\d+\.\d+)")
SUMS_LINE = re.compile(r"([0-9a-f]{64})  (\S+)")
SHA256 = re.compile(r"[0-9a-f]{64}")
# A generic client name: the request says nothing about the repository it comes from.
USER_AGENT = "release-publish-check (python-urllib)"
NOT_REPLACEABLE = (
    "A version on PyPI cannot be replaced, even after it is deleted. Cut a new release (a new "
    "version) instead. Nothing was uploaded."
)


class Refused(Exception):
    """The release must not be uploaded. The message says why and what to do."""


class _Unreadable(Exception):
    """The index gave no usable answer this time."""


@dataclass(frozen=True)
class Release:
    tag: str
    version: str
    files: dict  # file name -> path, for the wheel and the sdist
    digests: dict  # file name -> sha256


def normalize(name):
    """A project name as PyPI compares it (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _headers(text):
    message = email.parser.HeaderParser().parsestr(text)
    return message.get("Name"), message.get("Version")


def _wheel_metadata(path):
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", name)]
        if len(names) != 1:
            raise ValueError(f"expected one .dist-info/METADATA, found {len(names)}")
        return _headers(archive.read(names[0]).decode("utf-8"))


def _sdist_metadata(path):
    with tarfile.open(path, "r:gz") as archive:
        members = [m for m in archive.getmembers() if m.isfile() and re.fullmatch(r"[^/]+/PKG-INFO", m.name)]
        if len(members) != 1:
            raise ValueError(f"expected one PKG-INFO at the top of the sdist, found {len(members)}")
        return _headers(archive.extractfile(members[0]).read().decode("utf-8"))


def _read_sums(path):
    try:
        text = path.read_text(encoding="ascii")
    except FileNotFoundError:
        raise Refused(
            f"{path.name} is missing: the release has no checksum asset. Releases before v0.5.13 have "
            "none; only a release with SHA256SUMS and an attestation is published."
        ) from None
    except (OSError, UnicodeDecodeError) as error:
        raise Refused(f"Cannot read SHA256SUMS: {error}") from None
    sums = {}
    for number, line in enumerate(text.splitlines(), start=1):
        match = SUMS_LINE.fullmatch(line)
        if not match:
            raise Refused(f'SHA256SUMS line {number} is not "<sha256>  <file name>": {line!r}')
        digest, name = match.groups()
        if name in sums:
            raise Refused(f"SHA256SUMS lists {name} twice")
        sums[name] = digest
    return sums


def load_release(dist, project, tag):
    """Check the downloaded files and return them, or raise Refused."""
    dist = Path(dist)
    match = RELEASE_TAG.fullmatch(tag)
    if not match:
        raise Refused(f"{tag!r} is not a release tag (vX.Y.Z)")
    version = match.group(1)

    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        found = ", ".join(path.name for path in wheels + sdists) or "none"
        raise Refused(f"Expected exactly one wheel and one sdist on the release {tag}, found: {found}")
    wheel, sdist = wheels[0], sdists[0]
    files = {wheel.name: wheel, sdist.name: sdist}

    sums = _read_sums(dist / "SHA256SUMS")
    for name in sorted(files):
        if name not in sums:
            raise Refused(f"SHA256SUMS does not list {name}")
    for name in sorted(sums):
        if name not in files:
            raise Refused(f"SHA256SUMS lists {name}, which is not the release's wheel or sdist")
    digests = {name: _sha256(path) for name, path in files.items()}
    for name in sorted(files):
        if digests[name] != sums[name]:
            raise Refused(
                f"{name} does not match SHA256SUMS: the file's sha256 is {digests[name]}, "
                f"SHA256SUMS says {sums[name]}"
            )

    stem = f"{normalize(project).replace('-', '_')}-{version}"
    for kind, path, read in (("wheel", wheel, _wheel_metadata), ("sdist", sdist, _sdist_metadata)):
        try:
            name, got = read(path)
        except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile, tarfile.TarError) as error:
            raise Refused(f"Cannot read the metadata of the {kind} {path.name}: {error}") from None
        if name is None or normalize(name) != normalize(project):
            raise Refused(
                f"The {kind} {path.name} is the distribution {name!r} (metadata Name), not {project}. "
                f"A file built before the distribution was renamed cannot be published to PyPI as "
                f"{project}: publish a release that was cut after the rename."
            )
        if got != version:
            raise Refused(
                f"The {kind} {path.name} carries version {got!r} (metadata Version), not {version!r}, "
                f"the version of the tag {tag}. Only a release build is published."
            )
    if sdist.name != f"{stem}.tar.gz" or not wheel.name.startswith(f"{stem}-"):
        raise Refused(
            f"The file names {wheel.name} and {sdist.name} do not start with {stem}, the name and "
            f"version in their metadata."
        )
    return Release(tag=tag, version=version, files=files, digests=digests)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def fetch(url, timeout=30):
    """GET `url` and return (status, body). A redirect is returned, not followed."""
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read()


def _published(release, project, index, fetch):
    """What the index holds for the version: {file name: sha256}, or None when it answers 404."""
    url = f"{index.rstrip('/')}/pypi/{normalize(project)}/{release.version}/json"
    try:
        status, body = fetch(url)
    except OSError as error:
        raise _Unreadable(f"{url} could not be reached: {error}") from None
    if status == 404:
        return None
    if status != 200:
        raise _Unreadable(f"{url} answered {status}")
    try:
        files = json.loads(body)["urls"]
        published = {entry["filename"]: entry["digests"]["sha256"] for entry in files}
    except (ValueError, KeyError, TypeError):
        raise _Unreadable(f"{url} answered 200 with a body that is not a release's file list") from None
    if not isinstance(files, list) or not all(
        isinstance(name, str) and isinstance(digest, str) and SHA256.fullmatch(digest)
        for name, digest in published.items()
    ):
        raise _Unreadable(f"{url} answered 200 with a body that is not a release's file list")
    return published


def _compare(release, project, published):
    """Raise Refused when the index holds a file for the version that is not one of ours."""
    for name in sorted(published):
        ours = release.digests.get(name)
        if ours is None:
            raise Refused(
                f"PyPI has {name} for {project} {release.version}, and the release {release.tag} has no "
                f"such file. {NOT_REPLACEABLE}"
            )
        if published[name] != ours:
            raise Refused(
                f"PyPI already has {name} for {project} {release.version} with sha256 {published[name]}; "
                f"the release {release.tag} has sha256 {ours}. {NOT_REPLACEABLE}"
            )


def plan(release, project, index, fetch=fetch, attempts=3, delay=5.0, sleep=time.sleep):
    """The names of the files that may be uploaded, possibly none. Raises Refused."""
    for attempt in range(1, attempts + 1):
        try:
            published = _published(release, project, index, fetch)
            break
        except _Unreadable as error:
            if attempt == attempts:
                raise Refused(
                    f"Could not find out what PyPI holds for {project} {release.version}: {error} "
                    f"({attempts} attempt(s)). Nothing was uploaded. Run the Release workflow again with "
                    f"action=publish and tag {release.tag}."
                ) from None
            sleep(delay)
    if published is None:
        return sorted(release.files)
    _compare(release, project, published)
    return [name for name in sorted(release.files) if name not in published]


def confirm(release, project, index, fetch=fetch, attempts=20, delay=6.0, sleep=time.sleep):
    """Return once the index lists both files with our digests. Raises Refused after `attempts`."""
    state = "PyPI did not answer"
    for attempt in range(1, attempts + 1):
        try:
            published = _published(release, project, index, fetch)
        except _Unreadable as error:
            state = str(error)
        else:
            if published is None:
                state = f"PyPI does not list {project} {release.version}"
            else:
                _compare(release, project, published)
                missing = [name for name in sorted(release.files) if name not in published]
                if not missing:
                    return
                state = f"PyPI does not list {' or '.join(missing)}"
        if attempt < attempts:
            sleep(delay)
    raise Refused(
        f"After {attempts} checks: {state}. Look at https://pypi.org/project/{normalize(project)}/"
        f"{release.version}/#files. Running the Release workflow again with action=publish and tag "
        f"{release.tag} is safe: it uploads only what PyPI does not have."
    )


def _index_url(value):
    if value.startswith("https://") or re.match(r"http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", value):
        return value
    raise argparse.ArgumentTypeError("the index must be an https:// URL (http:// only for 127.0.0.1)")


def main(argv=None, fetch=fetch):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dist", required=True, type=Path, help="directory with the release's assets")
    common.add_argument("--project", required=True, help="the PyPI project name")
    common.add_argument("--tag", required=True, help="the release tag, vX.Y.Z")
    asks = argparse.ArgumentParser(add_help=False)
    asks.add_argument("--index", default="https://pypi.org", type=_index_url, help="base URL of the JSON API")
    asks.add_argument("--attempts", type=int, help="how many times to ask the index")
    asks.add_argument("--delay", type=float, help="seconds between two attempts")

    parser = argparse.ArgumentParser(description="Checks for the pypi job. Never uploads.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("verify", parents=[common], help="check the downloaded files")
    plan_parser = commands.add_parser("plan", parents=[common, asks], help="decide which files may be uploaded")
    plan_parser.add_argument("--output", required=True, type=Path, help="file to write the paths to upload to")
    commands.add_parser("confirm", parents=[common, asks], help="wait until the index lists both files")
    args = parser.parse_args(argv)

    try:
        release = load_release(args.dist, args.project, args.tag)
        if args.command == "verify":
            for name in sorted(release.files):
                print(f"{release.digests[name]}  {name}")
            print(f"Both files match SHA256SUMS. Name: {normalize(args.project)}. Version: {release.version}.")
            return 0

        retry = {key: value for key, value in (("attempts", args.attempts), ("delay", args.delay)) if value is not None}
        what = f"{args.project} {release.version}"
        if args.command == "plan":
            names = plan(release, args.project, args.index, fetch, **retry)
            have = [name for name in sorted(release.files) if name not in names]
            if not have:
                print(f"{what} is not on PyPI. To upload: {', '.join(names)}.")
            elif names:
                print(
                    f"::notice::PyPI already has {', '.join(have)} for {what} with the same sha256 (an "
                    f"earlier upload stopped part way). Uploading only {', '.join(names)}."
                )
            else:
                print(f"::notice::PyPI already has both files of {what} with the same sha256. Nothing to upload.")
            args.output.write_text("".join(f"{release.files[name]}\n" for name in names), encoding="utf-8")
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                    output.write(f"upload_count={len(names)}\n")
            return 0

        confirm(release, args.project, args.index, fetch, **retry)
        print(f"PyPI lists both files of {what} with this release's sha256.")
        return 0
    except Refused as refusal:
        print(f"::error::{refusal}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
