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


# --- the PyPI job ----------------------------------------------------------------------------------


def test_the_pypi_job_is_off_unless_the_variable_is_exactly_true():
    condition = _if("pypi")
    assert "vars.PYPI_PUBLISH_ENABLED == 'true'" in condition
    assert "vars.RELEASES_ENABLED == 'true'" in condition
    assert "PYPI_PUBLISH_ENABLED" not in JOBS["release"] + JOBS["reattach"] + JOBS["ci"]


def test_the_pypi_job_runs_after_the_release_job_and_never_after_a_cancel():
    pypi = JOBS["pypi"]
    assert re.search(r"^    needs: \[request, release\]$", pypi, flags=re.MULTILINE)
    condition = _if("pypi")
    assert condition.startswith("!cancelled()")
    assert "always()" not in condition
    assert "needs.request.result == 'success'" in condition
    assert "needs.release.result == 'success'" in condition
    assert "needs.release.outputs.released == 'true'" in condition
    assert "inputs.action == 'publish'" in condition
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


def test_the_pypi_job_has_the_oidc_permission_and_only_read_access_besides():
    pypi = JOBS["pypi"]
    block = re.search(r"^    permissions:\n((?:      \S.*\n)+)", pypi + "\n", flags=re.MULTILINE).group(1)
    permissions = dict(line.strip().split(": ") for line in block.splitlines())
    assert permissions == {"contents": "read", "id-token": "write"}
    assert _section(CODE, "permissions").strip() == "contents: read"


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
    assert "--signer-workflow" in pypi
    assert pypi.count("--project contexto-proxy") + pypi.count('--project "$PROJECT"') == 3


def test_the_upload_is_by_trusted_publishing_to_pypi_only():
    pypi = JOBS["pypi"]
    upload = pypi[pypi.index("uv publish") :].split("\n\n")[0]
    assert "--trusted-publishing always" in upload
    assert "--publish-url https://upload.pypi.org/legacy/" in upload
    assert "test.pypi.org" not in TEXT
    assert pypi.count("uv publish") == 1
    assert "steps.plan.outputs.upload_count != '0'" in pypi


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


def test_releasing_md_describes_every_script_and_switch():
    doc = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
    for script in sorted((ROOT / ".github" / "scripts").iterdir()):
        assert f"`.github/scripts/{script.name}`" in doc, script.name
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


def test_reattach_may_be_dispatched_from_another_ref_as_before():
    assert _request("reattach", "v0.5.24", "refs/heads/claude/some-branch").returncode == 0


def test_publish_is_refused_from_anywhere_but_main():
    result = _request("publish", "v0.6.0", "refs/heads/claude/some-branch")
    assert result.returncode == 1
    assert "main" in result.stdout


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
    assert pypi["permissions"] == {"contents": "read", "id-token": "write"}
    assert pypi["needs"] == ["request", "release"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {"group": "release", "cancel-in-progress": False}
