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
           200, no files at all             fail: the version's files were deleted
           anything else                    asked again, then fail without a plan
confirm  After the upload, wait until the index lists both files with this release's digests.

Every subcommand runs verify first. The upload itself is `uv publish` in the workflow.
"""

import argparse
import email.parser
import hashlib
import http.client
import json
import os
import re
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

RELEASE_TAG = re.compile(r"v([0-9]+\.[0-9]+\.[0-9]+)")
SUMS_LINE = re.compile(r"([0-9a-f]{64})  (\S+)")
SHA256 = re.compile(r"[0-9a-f]{64}")
# A generic client name: the request says nothing about the repository it comes from.
USER_AGENT = "release-publish-check (python-urllib)"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
# Nothing this script reads is large: the index's answer for one version is a few kilobytes, and
# so are METADATA, PKG-INFO and SHA256SUMS.
MAX_ANSWER = 4 * 1024 * 1024
MAX_METADATA = 1024 * 1024
MAX_SUMS = 64 * 1024
NOT_REPLACEABLE = "A version on PyPI cannot be replaced, even after it is deleted. Cut a new release (a new version)."


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


def _headers(file):
    data = file.read(MAX_METADATA + 1)
    if len(data) > MAX_METADATA:
        raise ValueError(f"the metadata is larger than {MAX_METADATA} bytes")
    message = email.parser.HeaderParser().parsestr(data.decode("utf-8"))
    return message.get("Name"), message.get("Version")


def _wheel_metadata(path, _stem):
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", name)]
        if len(names) != 1:
            raise ValueError(f"expected one .dist-info/METADATA, found {len(names)}")
        with archive.open(names[0]) as file:
            return _headers(file)


def _sdist_metadata(path, stem):
    # Only the sdist's own top directory: a PKG-INFO anywhere else says nothing about the sdist.
    wanted = f"{stem}/PKG-INFO"
    with tarfile.open(path, "r:gz") as archive:
        members = [member for member in archive.getmembers() if member.name == wanted and member.isfile()]
        if len(members) != 1:
            raise ValueError(f"expected the file {wanted}, found {len(members)}")
        with archive.extractfile(members[0]) as file:
            return _headers(file)


def _read_sums(path):
    try:
        if path.stat().st_size > MAX_SUMS:
            raise Refused(f"SHA256SUMS is larger than {MAX_SUMS} bytes; it lists two files")
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
    for path in (wheel, sdist, dist / "SHA256SUMS"):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise Refused(f"{path.name} on the release {tag} is not a regular file")

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
            name, got = read(path, stem)
        except (OSError, ValueError, KeyError, EOFError, RuntimeError, NotImplementedError, zipfile.BadZipFile,
                tarfile.TarError, zlib.error) as error:
            raise Refused(f"Cannot read the metadata of the {kind} {path.name}: {error}") from None
        if name is None:
            raise Refused(f"The {kind} {path.name} has no Name in its metadata. Only {project} is published.")
        if normalize(name) != normalize(project):
            why = f"Only {project} is published by this workflow."
            if normalize(name) == "contexto":
                why = (
                    f"A file built before the distribution was renamed cannot be published to PyPI as "
                    f"{project}: publish a release that was cut after the rename."
                )
            raise Refused(f"The {kind} {path.name} is the distribution {name!r} (metadata Name), not {project}. {why}")
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
    """GET `url` and return (status, body). A redirect is returned, not followed.

    Raises OSError or http.client.HTTPException when no answer arrives, when the status line or the
    headers are broken, or when a chunked body breaks off; OSError when the answer is larger than
    MAX_ANSWER, which is never read past. A body cut short of its Content-Length is returned as far
    as it came: http.client does not raise for it on a read with a limit. On a 200 the JSON check of
    the caller refuses it. A cut 404 is taken as 404, because its status line arrived whole and the
    body of a 404 is not read for anything.

    `timeout` bounds each read, not the whole answer: a body sent a byte at a time is never cut off
    here. The pypi job's `timeout-minutes` is the bound for that.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    handlers = [_NoRedirect]
    if urllib.parse.urlsplit(url).hostname in LOOPBACK:
        # A proxy from the environment must not get a request meant for this machine.
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout) as response:
            status, body = response.status, response.read(MAX_ANSWER + 1)
    except urllib.error.HTTPError as error:
        with error:
            status, body = error.code, error.read(MAX_ANSWER + 1)
    if len(body) > MAX_ANSWER:
        raise OSError(f"the answer is larger than {MAX_ANSWER} bytes")
    return status, body


def _published(release, project, index, fetch):
    """What the index holds for the version: {file name: sha256}, or None when it answers 404."""
    url = f"{index.rstrip('/')}/pypi/{normalize(project)}/{release.version}/json"
    try:
        status, body = fetch(url)
    except (OSError, http.client.HTTPException) as error:
        # No connection, a timeout, or an answer that was not HTTP or broke off in its status line,
        # its headers or a chunked body. A body cut short of its Content-Length is not an error here
        # (see fetch): a cut 200 fails the JSON check below, and a cut 404 is a 404.
        raise _Unreadable(f"no usable answer from {url}: {error!r}") from None
    if status == 404:
        return None
    if status != 200:
        raise _Unreadable(f"{url} answered {status}")
    try:
        files = json.loads(body)["urls"]
        published = {entry["filename"]: entry["digests"]["sha256"] for entry in files}
    except (ValueError, KeyError, TypeError, RecursionError):
        # RecursionError: JSON nested deeper than the parser's recursion limit.
        raise _Unreadable(f"{url} answered 200 with a body that is not a release's file list") from None
    # PyPI never lists a file name twice: an answer that does would hide one of the two digests.
    if not isinstance(files, list) or len(published) != len(files) or not all(
        isinstance(name, str) and isinstance(digest, str) and SHA256.fullmatch(digest)
        for name, digest in published.items()
    ):
        raise _Unreadable(f"{url} answered 200 with a body that is not a release's file list")
    return published


def _compare(release, project, published, after_upload=False):
    """Raise Refused when the index holds a file for the version that is not one of ours."""
    what = f"{project} {release.version}"
    if after_upload:
        # The upload step has run, so this run may have put files on PyPI.
        start = "After the upload, PyPI lists"
        end = f"{NOT_REPLACEABLE} Look at https://pypi.org/project/{normalize(project)}/{release.version}/#files."
    else:
        start = "PyPI already has"
        end = f"{NOT_REPLACEABLE} Nothing was uploaded."
    for name in sorted(published):
        ours = release.digests.get(name)
        if ours is None:
            raise Refused(f"{start} {name} for {what}, and the release {release.tag} has no such file. {end}")
        if published[name] != ours:
            raise Refused(
                f"{start} {name} for {what} with sha256 {published[name]}; the release {release.tag} has "
                f"sha256 {ours}. {end}"
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
    if not published:
        raise Refused(
            f"PyPI has the version {release.version} of {project} with no files. Its files were deleted, "
            f"and PyPI never accepts a deleted file's name again. {NOT_REPLACEABLE} Nothing was uploaded."
        )
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
            elif not published:
                state = f"PyPI lists no files for {project} {release.version}"
            else:
                _compare(release, project, published, after_upload=True)
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


def index_url(value):
    """An https:// URL, or http:// for this machine only. Anything else is refused."""
    try:
        parts = urllib.parse.urlsplit(value)
        host, _port = parts.hostname, parts.port
    except ValueError:
        parts, host = None, None
    if parts and host and "@" not in parts.netloc and not parts.query and not parts.fragment:
        if parts.scheme == "https" or (parts.scheme == "http" and host in LOOPBACK):
            return value
    raise argparse.ArgumentTypeError("the index must be an https:// URL (http:// only for 127.0.0.1 or localhost)")


def _count(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def _seconds(value):
    number = float(value)
    if not number >= 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def main(argv=None, fetch=fetch):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dist", required=True, type=Path, help="directory with the release's assets")
    common.add_argument("--project", required=True, help="the PyPI project name")
    common.add_argument("--tag", required=True, help="the release tag, vX.Y.Z")
    asks = argparse.ArgumentParser(add_help=False)
    asks.add_argument("--index", default="https://pypi.org", type=index_url, help="base URL of the JSON API")
    asks.add_argument("--attempts", type=_count, help="how many times to ask the index (1 or more)")
    asks.add_argument("--delay", type=_seconds, help="seconds between two attempts (0 or more)")

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
            try:
                args.output.write_text("".join(f"{release.files[name]}\n" for name in names), encoding="utf-8")
                if os.environ.get("GITHUB_OUTPUT"):
                    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                        output.write(f"upload_count={len(names)}\n")
            except OSError as error:
                raise Refused(f"Cannot write the plan: {error}. Nothing was uploaded.") from None
            return 0

        confirm(release, args.project, args.index, fetch, **retry)
        print(f"PyPI lists both files of {what} with this release's sha256.")
        return 0
    except Refused as refusal:
        print(f"::error::{refusal}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
