"""The shape of .github/workflows/release.yml, and the script that checks a dispatch's inputs.

The suite has no YAML dependency, so the workflow is read as text: jobs are split on their
two-space keys. A last test parses it for real when PyYAML happens to be installed.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
REQUEST = ROOT / ".github" / "scripts" / "release-request.sh"
TEXT = WORKFLOW.read_text(encoding="utf-8")


def _code(text):
    """The workflow without its comments (a `#` at the start of a line or after two spaces)."""
    return "\n".join(re.sub(r"(^|\s{2,})#.*$", "", line).rstrip() for line in text.splitlines())


def _section(text, key):
    """The lines of a top-level key, without the key line."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line == f"{key}:")
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\S", lines[i])), len(lines))
    return "\n".join(lines[start + 1 : end])


def _jobs(text):
    jobs, name = {}, None
    for line in _section(text, "jobs").splitlines():
        match = re.fullmatch(r"  ([A-Za-z0-9_-]+):", line)
        if match:
            name = match.group(1)
            jobs[name] = []
        elif name:
            jobs[name].append(line)
    return {name: "\n".join(lines) for name, lines in jobs.items()}


CODE = _code(TEXT)
JOBS = _jobs(CODE)


def _if(job):
    """A job's `if:` as one line."""
    lines = JOBS[job].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("    if:"))
    parts = [lines[start].removeprefix("    if:").strip().lstrip(">|-").strip()]
    for line in lines[start + 1 :]:
        if not line.startswith("      "):
            break
        parts.append(line.strip())
    return " ".join(part for part in parts if part)


def test_the_jobs_are_the_expected_ones():
    assert list(JOBS) == ["disabled", "request", "ci", "release", "reattach", "pypi", "pypi-disabled"]


# --- no release on a merge -------------------------------------------------------------------------


def test_there_is_no_push_trigger():
    assert not re.search(r"^\s*push\s*:", CODE, flags=re.MULTILINE)
    triggers = [line.strip() for line in _section(CODE, "on").splitlines() if re.fullmatch(r"  \S.*", line)]
    assert triggers == ["workflow_dispatch:"]
    for event in ("pull_request", "schedule", "workflow_run", "workflow_call", "issue_comment"):
        assert event not in _section(CODE, "on")


def test_the_dispatch_takes_an_action_and_an_optional_tag():
    on = _section(CODE, "on")
    action = on[on.index("      action:") : on.index("      tag:")]
    assert "type: choice" in action
    assert "default: release" in action
    assert re.findall(r"^\s+- (\S+)$", action, flags=re.MULTILINE) == ["release", "reattach", "publish"]
    tag = on[on.index("      tag:") :]
    assert "required: false" in tag
    assert "type: string" in tag


def test_every_job_but_the_notice_needs_releases_enabled():
    assert _if("disabled") == "vars.RELEASES_ENABLED != 'true'"
    for job in ("request", "ci", "release", "reattach", "pypi", "pypi-disabled"):
        assert "vars.RELEASES_ENABLED == 'true'" in _if(job), job


def test_each_action_runs_its_own_jobs():
    assert "inputs.action == 'release'" in _if("ci")
    assert "inputs.action == 'release'" in _if("release")
    assert "inputs.action == 'reattach'" in _if("reattach")
    assert "github.event_name" not in CODE


def test_the_release_job_is_gated_on_ci_and_the_checked_request():
    assert re.search(r"^    needs: request$", JOBS["ci"], flags=re.MULTILINE)
    assert "uses: ./.github/workflows/ci.yml" in JOBS["ci"]
    assert re.search(r"^    needs: ci$", JOBS["release"], flags=re.MULTILINE)
    assert re.search(r"^    needs: request$", JOBS["reattach"], flags=re.MULTILINE)
    assert "bash .github/scripts/release-request.sh" in JOBS["request"]


def test_the_inputs_reach_the_shell_only_through_the_environment():
    for line in CODE.splitlines():
        if "${{" in line and ("inputs." in line or "needs." in line or "steps." in line):
            assert re.match(r"^\s+([A-Za-z_]+|if|released|tag): ", line), line
    runs = re.findall(r"run: \|\n((?:\s{10,}.*\n?)+)", CODE)
    assert runs
    for script in runs:
        assert "${{" not in script, script


def test_the_changelog_guard_runs_before_anything_is_tagged():
    release = JOBS["release"]
    guard = release.index(".github/scripts/changelog-check.py")
    assert release.index("id: plan") < guard < release.index("--no-commit --tag --push")
    assert guard < release.index("actions/attest-build-provenance")


def test_the_release_never_commits_and_attests_before_it_uploads():
    release = JOBS["release"]
    assert "version --no-commit --tag --push --vcs-release --no-changelog" in release
    assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in release
    assert 'test "$(git rev-parse "$TAG^{commit}")" = "$GITHUB_SHA"' in release
    assert release.index("actions/attest-build-provenance") < release.index("gh release upload")
    reattach = JOBS["reattach"]
    assert reattach.index("actions/attest-build-provenance") < reattach.index("gh release upload")
    assert "bash .github/scripts/reattach-verify.sh" in reattach
    # semantic-release's changelog command may have rewritten CHANGELOG.md in the work tree.
    assert 'git checkout --quiet --force --detach "refs/tags/$TAG"' in reattach


# --- the PyPI job ----------------------------------------------------------------------------------


# The whole condition, not parts of it: one `||` for an `&&` turns a switch into a suggestion.
PUBLISHES = (
    "(inputs.action == 'publish'"
    " || (inputs.action == 'release' && needs.release.result == 'success'"
    " && needs.release.outputs.released == 'true'))"
)
PYPI_IF = (
    "!cancelled() && vars.RELEASES_ENABLED == 'true' && vars.PYPI_PUBLISH_ENABLED == 'true'"
    f" && needs.request.result == 'success' && {PUBLISHES}"
)
PYPI_DISABLED_IF = (
    "!cancelled() && vars.RELEASES_ENABLED == 'true' && vars.PYPI_PUBLISH_ENABLED != 'true'"
    f" && needs.request.result == 'success' && {PUBLISHES}"
)
TAG_EXPRESSION = "${{ inputs.action == 'publish' && inputs.tag || needs.release.outputs.tag }}"


def _expression(text):
    """An expression with its line breaks and the spaces inside parentheses taken out."""
    return re.sub(r"\s+", " ", text).replace("( ", "(").replace(" )", ")").strip()


def _steps(job):
    body = JOBS[job]
    return body[body.index("\n    steps:\n") :]


def test_the_pypi_job_is_off_unless_the_variable_is_exactly_true():
    assert _expression(_if("pypi")) == PYPI_IF
    assert "PYPI_PUBLISH_ENABLED" not in JOBS["release"] + JOBS["reattach"] + JOBS["ci"]


def test_the_two_pypi_conditions_differ_only_in_the_switch():
    assert _expression(_if("pypi-disabled")) == PYPI_DISABLED_IF
    assert PYPI_IF.replace("PYPI_PUBLISH_ENABLED == 'true'", "PYPI_PUBLISH_ENABLED != 'true'") == PYPI_DISABLED_IF


def test_the_other_conditions_are_the_whole_expected_expression():
    assert _if("disabled") == "vars.RELEASES_ENABLED != 'true'"
    assert _if("request") == "vars.RELEASES_ENABLED == 'true'"
    assert _if("ci") == "inputs.action == 'release' && vars.RELEASES_ENABLED == 'true'"
    assert _if("release") == "inputs.action == 'release' && vars.RELEASES_ENABLED == 'true'"
    assert _if("reattach") == "inputs.action == 'reattach' && vars.RELEASES_ENABLED == 'true'"


def test_the_published_tag_is_the_release_jobs_tag_or_the_input_on_publish():
    for job in ("pypi", "pypi-disabled"):
        assert re.findall(r"^\s+TAG: (.*)$", JOBS[job], flags=re.MULTILINE) == [TAG_EXPRESSION], job
    assert re.findall(r"^\s+TAG: (.*)$", JOBS["reattach"], flags=re.MULTILINE) == ["${{ inputs.tag }}"]
    release_tags = re.findall(r"^\s+TAG: (.*)$", JOBS["release"], flags=re.MULTILINE)
    assert release_tags and set(release_tags) == {"${{ steps.plan.outputs.tag }}"}
    outputs = JOBS["release"][JOBS["release"].index("    outputs:") : JOBS["release"].index("    steps:")]
    assert "      released: ${{ steps.plan.outputs.released }}" in outputs
    assert "      tag: ${{ steps.plan.outputs.tag }}" in outputs


def test_no_step_may_fail_and_let_the_job_go_on():
    assert "continue-on-error" not in TEXT
    assert "set +e" not in CODE
    # The one tolerated failure: no release yet, so there is no last released tag to print.
    tolerated = [line.strip() for line in _steps("release").splitlines() if "||" in line]
    assert tolerated == ["last=$($PSR version --print-last-released-tag || true)"]
    for job in ("request", "reattach", "pypi"):
        assert "||" not in _steps(job), job
    # A step's own condition never widens what runs: no status function below the job level.
    for job in ("request", "release", "reattach", "pypi"):
        for function in ("always()", "failure()", "cancelled()"):
            assert function not in _steps(job), (job, function)
    assert not re.search(r"^\s*set \+", CODE, flags=re.MULTILINE)
    assert "set +" not in CODE


def _step_list(job):
    """A job's steps, each as its lines without the indentation (comments already stripped)."""
    steps = []
    for line in _steps(job).splitlines()[1:]:
        if line.startswith("      - "):
            steps.append([line.strip()])
        elif line.strip() and steps:
            steps[-1].append(line.strip())
    return steps


def _step(job, name):
    matches = [step for step in _step_list(job) if f"name: {name}" in step[0]]
    assert len(matches) == 1, (job, name)
    return matches[0]


def _conditions(job):
    """(step's first line, its `if`) for every step of `job` that has one."""
    return [
        (step[0], line.removeprefix("if: "))
        for step in _step_list(job)
        for line in step
        if line.startswith("if: ")
    ]


RELEASED = "steps.plan.outputs.released == 'true'"


def test_each_jobs_step_conditions_are_exactly_the_expected_ones():
    # Per job: a condition copied into the wrong job names another job's step, is always false
    # there, and switches the step off without a sound.
    assert [condition for _, condition in _conditions("release")] == [
        RELEASED, RELEASED, RELEASED, f"{RELEASED} && hashFiles('dist/SHA256SUMS') != ''", RELEASED, RELEASED,
    ]
    first = [step[0] for step in _step_list("release")]
    unconditional = [name for name in first if name not in dict(_conditions("release"))]
    assert unconditional == [
        "- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "- uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7",
        "- name: Put the pinned Python first on PATH for the build",
        "- name: Compute the next version from Conventional Commits",
    ]
    assert _conditions("reattach") == [
        ("- name: Attest the build provenance of the rebuilt sdist and wheel", "hashFiles('dist/SHA256SUMS') != ''"),
    ]
    assert _conditions("pypi") == [("- name: Upload to PyPI by trusted publishing", "steps.plan.outputs.upload_count != '0'")]
    for job in ("disabled", "request", "pypi-disabled"):
        assert _conditions(job) == [], job


def test_every_run_block_is_bash_with_errexit():
    # The default shell of a run block is `bash -e {0}`. A `shell:` key or a `defaults:` block could
    # drop -e, and then a step's result is only that of its last command.
    assert not re.search(r"^\s*(- )?shell:", CODE, flags=re.MULTILINE)
    assert not re.search(r"^\s*defaults:", CODE, flags=re.MULTILINE)


def test_the_concurrency_block_and_the_pypi_timeout_are_as_expected():
    # A second dispatch waits; it never cancels a release between the tag push and the upload.
    lines = CODE.splitlines()
    start = lines.index("concurrency:")
    assert lines[start : start + 3] == ["concurrency:", "  group: release", "  cancel-in-progress: false"]
    assert _section(CODE, "concurrency").strip().splitlines() == ["group: release", "  cancel-in-progress: false"]
    assert re.findall(r"^    timeout-minutes: (.*)$", JOBS["pypi"], flags=re.MULTILINE) == ["20"]


def test_the_pypi_disabled_script_line_by_line():
    assert _step("pypi-disabled", "Report that PyPI publishing is off") == [
        "- name: Report that PyPI publishing is off",
        "env:",
        "ACTION: ${{ inputs.action }}",
        f"TAG: {TAG_EXPRESSION}",
        "run: |",
        'why="PyPI publishing is off: the repository variable PYPI_PUBLISH_ENABLED is not set to true."',
        'how="To turn it on, follow docs/pypi-publishing.md; then run this workflow with action=publish and tag $TAG."',
        'if [ "$ACTION" = publish ]; then',
        'echo "::error::$why $TAG was not published. $how"',
        "exit 1",
        "fi",
        'echo "::notice::$why $TAG is on GitHub Releases only. $how"',
    ]
    assert len(_step_list("pypi-disabled")) == 1


def test_the_pypi_jobs_checkout_download_and_tool_check_line_by_line():
    steps = _step_list("pypi")
    assert steps[0] == [
        "- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "with:",
        "fetch-depth: 0",
        "persist-credentials: false",
    ]
    assert steps[1] == [
        "- uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7",
        "with:",
        'version: "0.13.7"',
        "enable-cache: false",
    ]
    # No pipes: a run block has no pipefail, so `gh --version | head -1` would pass without gh.
    assert _step("pypi", "Check that gh, uv, python3 and sha256sum are on the runner") == [
        "- name: Check that gh, uv, python3 and sha256sum are on the runner",
        "run: |",
        "gh --version",
        "uv --version",
        "python3 --version",
        "sha256sum --version",
    ]
    assert _step("pypi", "Download the wheel, sdist and checksums from the GitHub Release") == [
        "- name: Download the wheel, sdist and checksums from the GitHub Release",
        "env:",
        "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
        "run: |",
        'mkdir "$RUNNER_TEMP/pypi-dist"',
        'gh release download "$TAG" --repo "$GITHUB_REPOSITORY" --dir "$RUNNER_TEMP/pypi-dist" \\',
        "--pattern '*.whl' --pattern '*.tar.gz' --pattern SHA256SUMS",
        'ls -l "$RUNNER_TEMP/pypi-dist"',
    ]


def test_the_scripts_run_with_an_isolated_python_and_the_guard_gets_the_last_release():
    calls = re.findall(r"python3( [^.\n]*)\.github/scripts/", CODE)
    assert calls and set(calls) == {" -I "}, calls
    assert JOBS["pypi"].count("python3 -I .github/scripts/pypi-publish.py ") == 3
    guard = _step("release", "Check that the changelog has a section for this version")
    assert guard[-1] == 'run: python3 -I .github/scripts/changelog-check.py "$TAG" --last-released "$LAST"'
    assert "LAST: ${{ steps.plan.outputs.last }}" in guard
    assert 'echo "last=$last" >> "$GITHUB_OUTPUT"' in JOBS["release"]


def test_the_release_and_reattach_jobs_build_with_the_same_pinned_python():
    # release-build.sh repacks the sdist with the python3 first on PATH; the sdist's bytes can depend
    # on that Python's version.
    pin = _step("release", "Put the pinned Python first on PATH for the build")
    assert pin == _step("reattach", "Put the pinned Python first on PATH for the build")
    assert pin == [
        "- name: Put the pinned Python first on PATH for the build",
        "run: |",
        "uv python install --no-bin 3.12",
        "python=$(uv python find --managed-python 3.12)",
        'mkdir "$RUNNER_TEMP/build-python"',
        'ln -s "$python" "$RUNNER_TEMP/build-python/python3"',
        'echo "$RUNNER_TEMP/build-python" >> "$GITHUB_PATH"',
        '"$RUNNER_TEMP/build-python/python3" --version',
    ]
    for job in ("release", "reattach"):
        names = [step[0] for step in _step_list(job)]
        assert names[1].startswith("- uses: astral-sh/setup-uv@") and names[2] == pin[0], job
        assert 'python-version: "3.12"' in _step_list(job)[1], job
    assert JOBS["reattach"].index(pin[0]) < JOBS["reattach"].index("bash .github/scripts/release-build.sh")
    assert JOBS["release"].index(pin[0]) < JOBS["release"].index("--no-commit --tag --push")


def test_the_pypi_job_runs_after_the_release_job_and_never_after_a_cancel():
    pypi = JOBS["pypi"]
    assert re.search(r"^    needs: \[request, release\]$", pypi, flags=re.MULTILINE)
    condition = _expression(_if("pypi"))
    assert condition.startswith("!cancelled() && ")
    assert "always()" not in condition
    # Nothing on the GitHub side waits for PyPI.
    for job in ("ci", "release", "reattach", "request", "disabled"):
        assert "pypi" not in re.findall(r"^    needs: (.*)$", JOBS[job], flags=re.MULTILINE).__str__(), job


def test_the_pypi_job_names_the_pypi_environment():
    pypi = JOBS["pypi"]
    assert re.search(
        r"^    environment:\n      name: pypi\n      url: https://pypi\.org/project/contexto-proxy/$",
        pypi,
        flags=re.MULTILINE,
    )
    for job, body in JOBS.items():
        if job != "pypi":
            assert "environment:" not in body, job


def _permissions(job):
    """A job's `permissions:` block as a dict."""
    lines = JOBS[job].splitlines()
    start = lines.index("    permissions:")
    granted = {}
    for line in lines[start + 1 :]:
        if not line.strip():
            continue  # a comment line, already stripped
        if not line.startswith("      "):
            break
        name, value = line.strip().split(": ")
        granted[name] = value
    return granted


def test_the_pypi_job_has_the_oidc_permission_and_only_read_access_besides():
    assert _permissions("pypi") == {"contents": "read", "id-token": "write", "attestations": "read"}
    assert _section(CODE, "permissions").strip() == "contents: read"
    for job in ("release", "reattach"):
        assert _permissions(job) == {"contents": "write", "id-token": "write", "attestations": "write"}, job
    for job in ("disabled", "request", "ci", "pypi-disabled"):
        assert "permissions:" not in JOBS[job], job


def test_no_job_but_those_that_sign_gets_an_oidc_token():
    for job, body in JOBS.items():
        assert ("id-token: write" in body) == (job in ("release", "reattach", "pypi")), job
    # The jobs that run semantic-release hold an OIDC token for the attestation, and no environment:
    # a trusted publisher registered with the environment name refuses their token.
    for job in ("release", "reattach"):
        assert "environment" not in JOBS[job]


# Keys and flags that would carry a credential. The workflow's own two: the gh token, and the
# permission to request an OIDC token.
ALLOWED_KEYS = {"GH_TOKEN", "id-token"}
CREDENTIAL_KEY = re.compile(r"^\s*(?:- )?([\w-]*(?:password|passwd|token|username|secret|api[_-]?key)[\w-]*):", re.I)
CREDENTIAL_WORD = re.compile(
    r"--(?:password|token|username|user)\b|\b(?:TWINE_\w+|UV_PUBLISH_\w+|PYPI_\w*TOKEN\w*|__token__|keyring)\b", re.I
)


def _credentials(text):
    """The credential-bearing keys and flags in the code of `text`; comments do not count."""
    found = []
    for line in _code(text).splitlines():
        key = CREDENTIAL_KEY.match(line)
        if key and key.group(1) not in ALLOWED_KEYS:
            found.append(key.group(1))
        found += CREDENTIAL_WORD.findall(line)
    return found


def test_there_is_no_pypi_token_or_password_anywhere():
    assert set(re.findall(r"secrets\.([A-Za-z0-9_]+)", TEXT)) == {"GITHUB_TOKEN"}
    assert _credentials(TEXT) == []
    assert "secrets." not in JOBS["pypi"].replace("secrets.GITHUB_TOKEN", "")
    assert "twine" not in CODE.lower()


def test_the_credential_check_reads_keys_and_flags_not_prose():
    assert _credentials("      # No API token or password is stored anywhere.\n      - name: no password here\n") == []
    for line in (
        "          UV_PUBLISH_PASSWORD: x",
        "          password: ${{ secrets.X }}",
        "            uv publish --token x",
        "            uv publish --username __token__",
        "          TWINE_USERNAME: x",
        "          PYPI_API_TOKEN: x",
        "          pypi-token: x",
        "            uv publish --password=x",
    ):
        assert _credentials(line + "\n"), line


def test_the_pypi_job_fetches_no_tool_at_run_time():
    pypi = JOBS["pypi"]
    for word in ("uvx", "uv run", "uv tool", "pip ", "semantic-release", "$PSR", "curl", "wget", "ensure-gh.sh",
                 "npm", "npx"):
        assert word not in pypi, word
    actions = re.findall(r"uses: (\S+)", pypi)
    assert [action.split("@")[0] for action in actions] == ["actions/checkout", "astral-sh/setup-uv"]


def test_the_pypi_job_publishes_the_release_assets_and_checks_them_first():
    pypi = JOBS["pypi"]
    order = [
        "bash .github/scripts/reattach-verify.sh",
        "gh release download",
        ".github/scripts/pypi-publish.py verify",
        "sha256sum --check --strict SHA256SUMS",
        "gh attestation verify",
        ".github/scripts/pypi-publish.py plan",
        "uv publish",
        ".github/scripts/pypi-publish.py confirm",
    ]
    positions = [pypi.index(step) for step in order]
    assert positions == sorted(positions)
    assert "uv build" not in pypi and "release-build.sh" not in pypi
    assert pypi.count("--project contexto-proxy") + pypi.count('--project "$PROJECT"') == 3
    assert pypi.count('--tag "$TAG"') == 3
    assert pypi.count('--dist "$RUNNER_TEMP/pypi-dist"') == 3


def test_the_attestation_must_come_from_release_yml_on_main_on_a_hosted_runner():
    pypi = _steps("pypi")
    assert pypi.count("gh attestation verify") == 1
    step = pypi[pypi.index("gh attestation verify") :].split("\n\n")[0]
    command = _expression(step[: step.index("\n          done")].replace("\\\n", " "))
    assert command == (
        'gh attestation verify "$file" --repo "$GITHUB_REPOSITORY"'
        ' --signer-workflow "$GITHUB_REPOSITORY/.github/workflows/release.yml"'
        " --source-ref refs/heads/main --deny-self-hosted-runners"
    )
    # Both files, each on its own: a failure for either stops the job.
    loop = pypi[: pypi.index("gh attestation verify")].rstrip().splitlines()[-1].strip()
    assert loop == 'for file in "$RUNNER_TEMP"/pypi-dist/*.whl "$RUNNER_TEMP"/pypi-dist/*.tar.gz; do'


def test_the_upload_is_by_trusted_publishing_to_pypi_only():
    pypi = JOBS["pypi"]
    assert "test.pypi.org" not in TEXT
    assert pypi.count("uv publish") == 1
    # Exactly the files the plan wrote: read into an array, no glob, nothing else on the line.
    step = pypi[pypi.rindex("      - name:", 0, pypi.index("uv publish")) :].split("\n\n")[0]
    lines = [line.strip() for line in step.splitlines()]
    assert lines == [
        "- name: Upload to PyPI by trusted publishing",
        "if: steps.plan.outputs.upload_count != '0'",
        "run: |",
        'mapfile -t files < "$RUNNER_TEMP/pypi-upload.txt"',
        "uv publish \\",
        "--trusted-publishing always \\",
        "--publish-url https://upload.pypi.org/legacy/ \\",
        "--check-url https://pypi.org/simple/ \\",
        "--no-attestations \\",
        '"${files[@]}"',
    ]
    plan = pypi[pypi.index("id: plan") : pypi.index("uv publish")]
    assert '--output "$RUNNER_TEMP/pypi-upload.txt"' in plan
    assert pypi.count("pypi-upload.txt") == 2


def test_a_run_with_publishing_off_says_so():
    condition = _if("pypi-disabled")
    assert "vars.PYPI_PUBLISH_ENABLED != 'true'" in condition
    assert condition.startswith("!cancelled()")
    assert "inputs.action == 'publish'" in condition
    assert "needs.release.outputs.released == 'true'" in condition
    body = JOBS["pypi-disabled"]
    assert "::notice::" in body
    assert "::error::" in body and "exit 1" in body
    assert "PYPI_PUBLISH_ENABLED" in body
    assert "id-token" not in body and "environment" not in body


def test_every_action_is_pinned_by_commit():
    uses = re.findall(r"uses: (\S+)(.*)", TEXT)
    assert uses
    for action, rest in uses:
        if action.startswith("./"):
            continue
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action), action
        assert re.search(r"#\s+v\d+\.\d+\.\d+", rest), action
    names = {action.split("@")[0] for action, _ in uses if not action.startswith("./")}
    assert names == {"actions/checkout", "astral-sh/setup-uv", "actions/attest-build-provenance"}
    assert len({action for action, _ in uses if action.startswith("astral-sh/setup-uv@")}) == 1
    assert len(set(re.findall(r'^\s+version: "([^"]+)"', TEXT, flags=re.MULTILINE))) == 1


def test_the_ci_workflow_uses_the_same_uv():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert set(re.findall(r"astral-sh/setup-uv@\S+", ci)) == set(re.findall(r"astral-sh/setup-uv@\S+", TEXT))
    uv_version = r'^\s+version: "([^"]+)"'
    assert set(re.findall(uv_version, ci, flags=re.MULTILINE)) == set(re.findall(uv_version, TEXT, flags=re.MULTILINE))


def test_the_owner_steps_name_what_the_workflow_uses():
    doc = (ROOT / "docs" / "pypi-publishing.md").read_text(encoding="utf-8")
    project = re.search(r"^      PROJECT: (\S+)$", JOBS["pypi"], flags=re.MULTILINE).group(1)
    assert project == "contexto-proxy"
    fields = {
        "PyPI Project Name": project,
        "Owner": "VentureForgre",
        "Repository name": "contexto",
        "Workflow name": WORKFLOW.name,
        "Environment name": "pypi",
    }
    for field, value in fields.items():
        assert f"| {field} | `{value}` |" in doc, field
    assert "gh variable set PYPI_PUBLISH_ENABLED --body true" in doc
    assert "-f action=publish -f tag=" in doc
    # The owner checks the environment and its branch rule before registering the publisher.
    check = doc.index("gh api repos/VentureForgre/contexto/environments/pypi/deployment-branch-policies")
    assert doc.index("gh api repos/VentureForgre/contexto/environments/pypi ") < check
    assert check < doc.index("Add a new pending publisher") < doc.index("gh variable set PYPI_PUBLISH_ENABLED")


def test_the_repositorys_changelog_has_no_semantic_release_marker():
    # semantic-release's changelog command, which the reattach job runs, inserts its own text at
    # this line and leaves a file without it untouched.
    changelog = ROOT / "CHANGELOG.md"
    assert changelog.is_file()
    assert not re.search(r"<!--\s*version list\s*-->", changelog.read_text(encoding="utf-8"), flags=re.IGNORECASE)


def test_releasing_md_describes_every_script_and_switch():
    doc = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
    # The release's scripts by name. Other scripts may live in the same directory.
    for name in ("release-request.sh", "changelog-check.py", "release-build.sh", "reattach-verify.sh",
                 "pypi-publish.py", "ensure-gh.sh"):
        assert (ROOT / ".github" / "scripts" / name).is_file(), name
        assert f"`.github/scripts/{name}`" in doc, name
        assert f".github/scripts/{name}" in TEXT, name
    for word in ("RELEASES_ENABLED", "PYPI_PUBLISH_ENABLED", "-f action=release", "-f action=reattach",
                 "-f action=publish", "docs/pypi-publishing.md"):
        assert word in doc, word
    assert "Every push to `main`" not in doc


# --- the dispatch's inputs ---------------------------------------------------------------------------

MAIN = "refs/heads/main"


def _request(action, tag, ref):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not on PATH")
    return subprocess.run([bash, str(REQUEST), action, tag, ref], capture_output=True, text=True)


def test_a_release_from_main_without_a_tag_is_accepted():
    result = _request("release", "", MAIN)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("ref", ["refs/heads/claude/some-branch", "refs/tags/v0.5.24", "refs/heads/main2", ""])
def test_a_release_from_anywhere_but_main_is_refused(ref):
    result = _request("release", "", ref)
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert "main" in result.stdout


def test_a_release_with_a_tag_is_refused():
    result = _request("release", "v0.6.0", MAIN)
    assert result.returncode == 1
    assert "tag" in result.stdout and "empty" in result.stdout


@pytest.mark.parametrize("action", ["reattach", "publish"])
def test_recovery_needs_a_release_tag(action):
    assert _request(action, "v0.5.24", MAIN).returncode == 0
    for tag in ("", "0.5.24", "v0.5", "main", "v0.5.24; rm -rf .", "v0.5.24\nv0.5.23", "$(id)", "v0.5.24-rc1"):
        result = _request(action, tag, MAIN)
        assert result.returncode == 1, tag
        assert "::error::" in result.stdout


@pytest.mark.parametrize("action", ["reattach", "publish"])
@pytest.mark.parametrize("ref", ["refs/heads/claude/some-branch", "refs/tags/v0.6.0", "refs/heads/main2", ""])
def test_recovery_is_refused_from_anywhere_but_main(action, ref):
    result = _request(action, "v0.6.0", ref)
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert f"--ref main -f action={action} -f tag=v0.6.0" in result.stdout


def test_an_unknown_action_is_refused():
    assert _request("delete", "", MAIN).returncode == 1
    assert _request("", "", MAIN).returncode == 1


# --- the tag check of the reattach and pypi jobs -------------------------------------------------------

VERIFY = ROOT / ".github" / "scripts" / "reattach-verify.sh"


def _git_env(home):
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }


def _git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=cwd, env=_git_env(cwd), capture_output=True, text=True)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout.strip()


@pytest.fixture
def history(tmp_path):
    """A bare origin whose main has a squash-style commit, a merge commit and a side branch's commit.

        main (first parent):  A - B ------- M - C
                                \\         /
        side:                    S1 ---- S2           X: on a branch that never reached main
    """
    if shutil.which("git") is None or shutil.which("bash") is None:
        pytest.skip("git or bash is not on PATH")
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    _git(tmp_path, "-c", "init.defaultBranch=main", "init", "-q", "--bare", str(origin))
    _git(tmp_path, "-c", "init.defaultBranch=main", "init", "-q", str(work))
    _git(work, "remote", "add", "origin", str(origin))
    commits = {}

    def commit(name):
        (work / "file").write_text(name, encoding="utf-8")
        _git(work, "add", "file")
        _git(work, "commit", "-q", "-m", name)
        commits[name] = _git(work, "rev-parse", "HEAD")

    commit("A")
    _git(work, "switch", "-q", "-c", "side")
    commit("S1")
    commit("S2")
    _git(work, "switch", "-q", "main")
    commit("B")
    _git(work, "merge", "-q", "--no-ff", "-X", "theirs", "-m", "M", "side")
    commits["M"] = _git(work, "rev-parse", "HEAD")
    commit("C")
    _git(work, "switch", "-q", "-c", "unmerged", commits["B"])
    commit("X")
    tags = {"v1.0.0": "B", "v1.1.0": "S1", "v1.2.0": "M", "v1.3.0": "C", "v1.4.0": "S2", "v2.0.0": "X", "v0.1.0": "A"}
    for tag, name in tags.items():
        _git(work, "tag", "-a", "-m", tag, tag, commits[name])
    _git(work, "push", "-q", "origin", "main", "side", "unmerged", "--tags")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", "--branch", "main", str(origin), str(checkout))
    return {"origin": origin, "work": work, "checkout": checkout, "commits": commits}


def _verify(checkout, tag):
    return subprocess.run(
        ["bash", str(VERIFY), tag], cwd=checkout, env=_git_env(checkout), capture_output=True, text=True
    )


@pytest.mark.parametrize("tag", ["v0.1.0", "v1.0.0", "v1.2.0", "v1.3.0"])
def test_a_tag_on_mains_first_parent_chain_passes(history, tag):
    result = _verify(history["checkout"], tag)
    assert result.returncode == 0, result.stderr
    assert f"{tag} -> " in result.stdout and "first-parent chain" in result.stdout


@pytest.mark.parametrize("tag", ["v1.1.0", "v1.4.0"])
def test_a_tag_on_a_commit_that_reached_main_through_a_merge_commit_is_refused(history, tag):
    checkout = history["checkout"]
    # It is an ancestor of main: the older check let it through.
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", f"refs/tags/{tag}", "refs/remotes/origin/main"], cwd=checkout,
        env=_git_env(checkout),
    ).returncode == 0
    result = _verify(checkout, tag)
    assert result.returncode == 1
    assert "first-parent chain" in result.stderr and "refusing" in result.stderr


def test_a_tag_off_main_or_missing_is_refused(history):
    result = _verify(history["checkout"], "v2.0.0")
    assert result.returncode == 1 and "not on main" in result.stderr
    result = _verify(history["checkout"], "v9.9.9")
    assert result.returncode == 1 and "does not exist" in result.stderr
    result = _verify(history["checkout"], "main")
    assert result.returncode == 1 and "Not a release tag" in result.stderr


def test_a_tag_named_like_the_remote_branch_does_not_stand_in_for_main(history):
    # git resolves the short name origin/main to refs/tags/origin/main before the remote branch.
    work = history["work"]
    _git(work, "tag", "origin/main", history["commits"]["S2"])
    _git(work, "push", "-q", "origin", "refs/tags/origin/main")
    assert _verify(history["checkout"], "v1.4.0").returncode == 1
    assert _verify(history["checkout"], "v2.0.0").returncode == 1
    assert _verify(history["checkout"], "v1.3.0").returncode == 0


def test_main_is_read_from_the_remote_after_a_fetch(history):
    # The job's checkout may be older than main: the script fetches main before it checks.
    work, checkout = history["work"], history["checkout"]
    _git(work, "switch", "-q", "main")
    (work / "file").write_text("D", encoding="utf-8")
    _git(work, "commit", "-q", "-am", "D")
    _git(work, "tag", "-a", "-m", "v1.5.0", "v1.5.0")
    _git(work, "push", "-q", "origin", "main", "v1.5.0")
    result = _verify(checkout, "v1.5.0")
    assert result.returncode == 0, result.stderr


def test_the_tag_check_runs_under_strict_bash():
    script = VERIFY.read_text(encoding="utf-8")
    assert "set -euo pipefail" in script
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    assert 'MAIN="refs/remotes/origin/main"' in code
    assert "origin/main" not in code.replace("refs/remotes/origin/main", "")
    assert 'git rev-list --first-parent "$MAIN" | grep -cx -- "$commit" || true' in code
    assert 'if [ "$on_chain" != 1 ]; then' in code


# --- with a YAML parser, when there is one -----------------------------------------------------------


def test_the_workflow_parses_as_yaml():
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(TEXT)
    triggers = workflow.get("on", workflow.get(True))
    assert list(triggers) == ["workflow_dispatch"]
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["action"]["options"] == ["release", "reattach", "publish"]
    assert inputs["action"]["default"] == "release"
    assert inputs["tag"]["required"] is False
    assert list(workflow["jobs"]) == list(JOBS)
    pypi = workflow["jobs"]["pypi"]
    assert pypi["environment"] == {"name": "pypi", "url": "https://pypi.org/project/contexto-proxy/"}
    assert pypi["permissions"] == {"contents": "read", "id-token": "write", "attestations": "read"}
    assert _expression(pypi["if"]) == PYPI_IF
    assert _expression(workflow["jobs"]["pypi-disabled"]["if"]) == PYPI_DISABLED_IF
    assert pypi["env"] == {"PROJECT": "contexto-proxy", "TAG": TAG_EXPRESSION}
    assert all("continue-on-error" not in step for job in workflow["jobs"].values() for step in job.get("steps", []))
    assert pypi["needs"] == ["request", "release"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {"group": "release", "cancel-in-progress": False}
