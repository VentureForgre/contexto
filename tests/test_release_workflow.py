"""The shape of .github/workflows/release.yml, and the script that checks a dispatch's inputs.

The suite has no YAML dependency, so the workflow is read as text: jobs are split on their
two-space keys. A last test parses it for real when PyYAML happens to be installed.
"""

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
    step_conditions = set(re.findall(r"^        if: (.*)$", _steps("release") + _steps("pypi"), flags=re.MULTILINE))
    assert step_conditions == {
        "steps.plan.outputs.released == 'true'",
        "steps.plan.outputs.released == 'true' && hashFiles('dist/SHA256SUMS') != ''",
        "steps.plan.outputs.upload_count != '0'",
    }


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


def test_there_is_no_pypi_token_or_password_anywhere():
    assert set(re.findall(r"secrets\.([A-Za-z0-9_]+)", TEXT)) == {"GITHUB_TOKEN"}
    lowered = TEXT.lower()
    for word in ("twine", "uv_publish_token", "uv_publish_password", "uv_publish_username", "password", "__token__",
                 "pypi_token", "pypi_api", "--token", "--username", "keyring"):
        assert word not in lowered, word
    assert "secrets." not in JOBS["pypi"].replace("secrets.GITHUB_TOKEN", "")


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
