#!/usr/bin/env bash
# Install a version-pinned actionlint binary for the Workflow Lint job (#1214).
#
# Usage:
#   bash scripts/install_actionlint.sh <version> [dest_dir]
#
# <version> is required and must be an exact "major.minor.patch" -- rhysd's
# installer treats an omitted version as "latest", and silently unpinning CI is
# the opposite of what docs/deployment.md's "CI pin maintenance" section asks
# for. dest_dir is where the fetched installer script is parked; it defaults to
# $RUNNER_TEMP, then the current directory. The actionlint binary itself lands
# wherever the installer puts it (the current directory), and the installer
# writes its path to $GITHUB_OUTPUT as `executable` -- unchanged from when this
# ran inline in .github/workflows/actionlint.yml.
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
#      (visible in `ps`) or the log.
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
# later as a confusing "command not found" at the lint step.
#
# Note the release-asset download the fetched installer then performs is a
# different, less contended surface than raw.githubusercontent.com; the raw
# fetch of the installer itself is the per-IP exposure this script addresses.

set -euo pipefail

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
    echo "Error: actionlint version required (usage: $0 <version> [dest_dir])" >&2
    exit 2
fi

DEST_DIR="${2:-${RUNNER_TEMP:-$(pwd)}}"
INSTALLER="$DEST_DIR/download-actionlint.bash"

# Capture the ambient GitHub token (GH_TOKEN wins, matching gh's own precedence
# -- `gh help environment`) and immediately drop both names, so neither curl nor
# the downloaded installer inherits it. Only the fetch needs the value, and it
# travels from this variable, never the environment.
AUTH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
unset GITHUB_TOKEN GH_TOKEN

INSTALLER_URL="https://raw.githubusercontent.com/rhysd/actionlint/v${VERSION}/scripts/download-actionlint.bash"
# --max-time/--retry mirror the pin in scripts/generate_api_models.sh for this
# same surface; curl's --retry also covers 429/5xx and honors Retry-After.
CURL_OPTS=(-sSfL --max-time 30 --retry 3)
# One definition of the URL/output pairing, called with or without the header
# argument, so the two ways in can't drift apart.
_fetch_installer() { curl "${CURL_OPTS[@]}" "$@" "$INSTALLER_URL" -o "$INSTALLER"; }

if [[ -n "$AUTH_TOKEN" ]]; then
    echo "  Authenticated download: sending Authorization header (token value not logged)." >&2
    if ! printf 'Authorization: Bearer %s\n' "$AUTH_TOKEN" | _fetch_installer -H @-; then
        echo "  Authenticated download failed; retrying anonymously (is the ambient token stale?)..." >&2
        _fetch_installer
    fi
else
    # Say so: without this line a dropped `GH_TOKEN:` in the workflow step
    # silently reverts CI to the anonymous path this script exists to leave.
    echo "  No GH_TOKEN/GITHUB_TOKEN set: downloading anonymously (shared per-IP rate budget)." >&2
    _fetch_installer
fi

# Pass the same VERSION through, so the installer tag and the installed binary
# stay in lockstep.
bash "$INSTALLER" "$VERSION"
