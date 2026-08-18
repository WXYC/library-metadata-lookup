#!/usr/bin/env bash
# Install a version-pinned actionlint binary for the Workflow Lint job (#1214).
#
# Usage:
#   bash scripts/install_actionlint.sh <version>
#
# <version> is required and must be an exact "major.minor.patch". It is
# interpolated into the raw URL below, so an omitted one fetches nothing; and
# were the wrapper to hand the installer an empty argument, rhysd's script falls
# back to its own manually-maintained default (the v1.7.12 script hardcodes
# version="1.7.11"; "latest" is a synonym for that default, not a network
# lookup), which would silently decouple the installed binary from the tag this
# script fetched -- the opposite of what docs/deployment.md's "CI pin
# maintenance" section asks for.
#
# The fetched installer script is parked in $RUNNER_TEMP (the current directory
# when run outside Actions). The actionlint binary itself lands wherever the
# installer puts it (the current directory), and the installer writes its path
# to $GITHUB_OUTPUT as `executable` -- unchanged from when this ran inline in
# .github/workflows/actionlint.yml.
#
# WHY THIS IS A SCRIPT AND NOT AN INLINE `run:` BLOCK: so the token-safety and
# retry properties below can be pinned by tests (tests/unit/
# test_install_actionlint_auth.py) rather than reviewed by eye. Same shape as
# scripts/check_plan_links.sh, called from .github/workflows/plan-links.yml.
#
# The token flow mirrors scripts/generate_api_models.sh, whose header holds the
# CANONICAL RATIONALE for all of it (#1205) -- read that one first; only what is
# specific to this script is restated here:
#
#   1. Anonymous raw.githubusercontent.com requests share a per-IP rate budget
#      across the Actions runner pool and intermittently 429. Authenticated ones
#      get a per-token budget. This job's `paths` filter fires precisely on PRs
#      touching .github/workflows/**, so a 429 here reads as "your CI change is
#      broken" rather than as runner-IP luck -- the reason it is worth fixing
#      even though the job is otherwise sub-second and cheap to re-run.
#   2. The header rides curl's stdin (-H @-), so the token never reaches argv
#      (visible in `ps`) or this script's own output. It is NOT proof against
#      `bash -x`, which traces the assignment and the printf; in Actions the
#      runner masks github.token in logs, so that exposure is a developer
#      tracing locally with their own PAT, to their own terminal.
#   3. The token is dropped from the environment before the fetch. This matters
#      MORE here than in the codegen script: the child process is a third-party
#      script fetched over the network moments earlier. Handing it the job's
#      GITHUB_TOKEN would widen the blast radius of an upstream compromise from
#      "lints our workflows" to "holds our token". It does not need one -- it
#      resolves the release asset URL by string construction and never calls
#      api.github.com.
#   4. A failed authenticated attempt retries once anonymously, unconditionally.
#      Pre-#1214 anonymous behavior is the floor in every environment.
#
# Fetch-to-file, never `bash <(curl ...)`: process substitution discards curl's
# exit code, so a transient fetch failure would pass green here and surface
# later as a confusing "command not found" at the lint step. The non-empty check
# after the fetch closes the same gap for a 200 carrying an empty body, which
# curl reports as success and which would otherwise reach that identical
# confusing failure.
#
# Note the release-asset download the fetched installer then performs is a
# different, less contended surface than raw.githubusercontent.com; the raw
# fetch of the installer itself is the per-IP exposure this script addresses.

set -euo pipefail

# Capture the ambient GitHub token (GH_TOKEN wins, matching gh's own precedence
# -- `gh help environment`) and immediately drop both names, so neither curl nor
# the downloaded installer inherits it. Only the fetch needs the value, and it
# travels from this variable, never the environment. Hoisted above every other
# statement so the property is structural rather than positional: nothing that
# runs before this line can leak what it has not yet had a chance to scrub.
AUTH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
unset GITHUB_TOKEN GH_TOKEN

VERSION="${1:-}"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: version must be exact major.minor.patch, got '$VERSION' (usage: $0 <version>)" >&2
    exit 2
fi

INSTALLER="${RUNNER_TEMP:-$PWD}/download-actionlint.bash"

INSTALLER_URL="https://raw.githubusercontent.com/rhysd/actionlint/v${VERSION}/scripts/download-actionlint.bash"
# One definition of the whole fetch -- options, URL and output path -- called
# with or without the header argument, so the three ways in can't drift apart.
# --max-time/--retry mirror the pin in scripts/generate_api_models.sh for this
# same surface; curl's --retry also covers 429/5xx and honors Retry-After.
# --retry-max-time bounds the whole ladder: without it, two arms x four attempts
# x 30s could burn ~4 minutes of billed CI against a hard outage before failing
# anyway, which is worse than the pre-#1214 fail-in-seconds it replaced.
_fetch_installer() {
    curl -sSfL --max-time 30 --retry 3 --retry-max-time 60 "$@" "$INSTALLER_URL" -o "$INSTALLER"
}

if [[ -n "$AUTH_TOKEN" ]]; then
    echo "  Authenticated download: sending Authorization header (token value not logged)." >&2
    if ! printf 'Authorization: Bearer %s\n' "$AUTH_TOKEN" | _fetch_installer -H @-; then
        # ::warning:: so this reaches the run summary. Without it the fallback
        # is invisible: a permanently-invalid token would go green forever on
        # the anonymous path, spending TWO requests against the shared per-IP
        # budget where pre-#1214 spent one. Deliberately does not assert a
        # cause -- DNS, a timeout and a stale token all land here.
        echo "::warning::actionlint installer: authenticated download failed; falling back to the shared anonymous rate budget." >&2
        _fetch_installer
    fi
else
    # Say so: without this line a dropped `GH_TOKEN:` in the workflow step
    # silently reverts CI to the anonymous path this script exists to leave.
    echo "  No GH_TOKEN/GITHUB_TOKEN set: downloading anonymously (shared per-IP rate budget)." >&2
    _fetch_installer
fi

if [[ ! -s "$INSTALLER" ]]; then
    echo "Error: downloaded installer is empty ($INSTALLER)" >&2
    exit 1
fi

# Pass the same VERSION through, so the installer tag and the installed binary
# stay in lockstep.
bash "$INSTALLER" "$VERSION"
