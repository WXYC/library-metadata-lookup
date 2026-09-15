#!/usr/bin/env bash
# Generate Python Pydantic v2 models from the wxyc-shared api.yaml OpenAPI spec.
#
# Looks for api.yaml in a sibling wxyc-shared directory first, then falls back
# to downloading from GitHub. The generated file is committed to git so that
# normal CI jobs don't need the codegen toolchain.
#
# Usage:
#   bash scripts/generate_api_models.sh                  # sibling checkout, else wxyc-shared main
#   bash scripts/generate_api_models.sh --ref <sha>      # pin to an upstream revision
#   bash scripts/generate_api_models.sh --download-only  # stop after fetching api.yaml (test hook)
#   WXYC_SHARED_REF=<sha> bash scripts/generate_api_models.sh
#
# An explicit --ref (or WXYC_SHARED_REF) always downloads that exact revision,
# bypassing any sibling checkout, so the regen is reproducible. Unpinned is the
# deliberate default for the Codegen Freshness CI job, whose whole purpose is to
# diff the committed snapshot against whatever upstream main currently says --
# pinning it there would silence the drift signal. See WXYC/wxyc-shared#319 and
# docs/scripts.md.
#
# CANONICAL RATIONALE for the token flow (#1205) -- ci.yml and the docs point
# here rather than restating it. When GH_TOKEN or GITHUB_TOKEN is set (GH_TOKEN
# wins, matching gh's own precedence -- `gh help environment`), the GitHub
# download authenticates via a Bearer Authorization header: anonymous
# raw.githubusercontent.com requests share a per-IP rate budget across the
# Actions runner pool and intermittently 429, while authenticated ones get a
# per-token budget. Three properties, each pinned by a test in
# tests/unit/test_generate_api_models_auth.py:
#
#   1. The header rides curl's stdin (-H @-), so the token value never reaches
#      argv (visible in `ps`) or the log.
#   2. Both variables are unset at the top of the script -- above the
#      source-resolution branch, so no child process inherits the token on
#      EITHER arm. The sibling-checkout arm never downloads, so a scrub placed
#      inside the download branch would miss the default local invocation.
#   3. A failed authenticated attempt retries once anonymously, unconditionally.
#      The motivating case is a stale token (GitHub 404s -- not 401s -- raw
#      requests carrying one, with no server-side anonymous fallback), but the
#      retry is not gated on that status: pre-#1205 anonymous behavior is the
#      floor in every environment. On a transient failure it costs one extra
#      attempt, which is the intended trade -- the anonymous per-IP budget is a
#      different bucket from the per-token one, so it can still succeed after
#      an authenticated 429.
#
# Unset-token runs are unchanged, apart from a stderr note saying the download
# is anonymous -- so losing the CI token surfaces as a visible regression
# rather than as a return of the original intermittent 429.
#
# --download-only stops after resolving/fetching api.yaml, skipping codegen and
# formatting. It exists for the unit tests covering the download branch
# (tests/unit/test_generate_api_models_auth.py).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT="$PROJECT_DIR/generated/api_models.py"

# The five streaming URL fields WXYC/wxyc-shared#428 annotated with
# `format: uri`, which datamodel-codegen maps to pydantic AnyUrl. Held at `str`
# below; see pin_streaming_url_fields_to_str().
STREAMING_URL_FIELDS=(
    spotify_url
    apple_music_url
    youtube_music_url
    bandcamp_url
    soundcloud_url
)

REF="${WXYC_SHARED_REF:-}"
DOWNLOAD_ONLY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --download-only)
            DOWNLOAD_ONLY=1
            shift
            ;;
        --ref)
            [[ $# -ge 2 ]] || { echo "Error: --ref requires a value" >&2; exit 2; }
            REF="$2"
            shift 2
            ;;
        --ref=*)
            REF="${1#--ref=}"
            shift
            ;;
        -h|--help)
            # Print the header comment block, skipping the shebang. Derived
            # rather than a fixed line range so editing the header can't
            # silently truncate --help mid-sentence.
            awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
            exit 0
            ;;
        *)
            echo "Error: unknown argument '$1' (see --help)" >&2
            exit 2
            ;;
    esac
done

# Capture the ambient GitHub token (#1205; GH_TOKEN wins, matching gh's own
# precedence -- `gh help environment`) and immediately drop both names from the
# environment. Done HERE, before the source resolution below, because the scrub
# is a property of the whole script rather than of the download arm: the
# sibling-checkout arm never downloads, and leaving the unset inside the
# download branch would hand the token to datamodel-codegen, ruff, and their
# whole dependency tree on the default local invocation. Only the download
# needs the value, and it travels from this variable, never the environment.
AUTH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
unset GITHUB_TOKEN GH_TOKEN

# Resolve api.yaml source. Inside a worktree, `git rev-parse --show-toplevel`
# returns the worktree path, not the main repo, so use --git-common-dir to find
# the real repo root.
SIBLING_PATH="$PROJECT_DIR/../wxyc-shared/api.yaml"
if MAIN_GIT_DIR="$(cd "$PROJECT_DIR" && git rev-parse --git-common-dir 2>/dev/null)"; then
    if [[ "$MAIN_GIT_DIR" != /* ]]; then
        MAIN_GIT_DIR="$PROJECT_DIR/$MAIN_GIT_DIR"
    fi
    MAIN_REPO_ROOT="$(cd "$MAIN_GIT_DIR/.." && pwd)"
    SIBLING_PATH="$MAIN_REPO_ROOT/../wxyc-shared/api.yaml"
fi

if [[ -z "$REF" && -f "$SIBLING_PATH" ]]; then
    API_YAML="$SIBLING_PATH"
    # Report the checkout's revision: generating against a stale or unpulled
    # sibling silently drops classes that only exist upstream, and the symptom
    # (a CI drift failure) points at api.yaml rather than at the checkout.
    SIBLING_DIR="$(cd "$(dirname "$SIBLING_PATH")" && pwd)"
    SIBLING_REV="$(git -C "$SIBLING_DIR" describe --always --dirty 2>/dev/null || echo "unknown")"
    SIBLING_BRANCH="$(git -C "$SIBLING_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")"
    echo "Using local api.yaml: $API_YAML ($SIBLING_BRANCH @ $SIBLING_REV)"
    echo "  Pull it first if it may be behind upstream main." >&2
else
    API_YAML="$(mktemp)"
    trap 'rm -f "$API_YAML"' EXIT
    if [[ -n "$REF" ]]; then
        echo "Downloading api.yaml from GitHub, pinned to '$REF'..."
    else
        REF="main"
        echo "Downloading api.yaml from GitHub..."
        echo "  Unpinned: resolving wxyc-shared 'main' as of now. Pass --ref <sha> to pin." >&2
    fi
    API_YAML_URL="https://raw.githubusercontent.com/WXYC/wxyc-shared/${REF}/api.yaml"
    # --max-time/--retry mirror wxyc-shared's generate-python-models.sh pin for
    # this same download; curl's --retry also covers 429/5xx, honoring
    # Retry-After, which directly serves the #1205 goal.
    CURL_OPTS=(-sSfL --max-time 30 --retry 3)
    # One definition of the URL/output pairing, called with or without the
    # header argument, so the three ways in can't drift apart.
    _fetch_api_yaml() { curl "${CURL_OPTS[@]}" "$@" "$API_YAML_URL" -o "$API_YAML"; }
    if [[ -n "$AUTH_TOKEN" ]]; then
        echo "  Authenticated download: sending Authorization header (token value not logged)." >&2
        # -H @- reads the header from stdin, keeping the token off the process
        # argv (visible in `ps` on shared hosts). The retry below is
        # deliberately UNconditional rather than gated on the stale-token 404
        # described in the header: pre-#1205 anonymous behavior is the floor in
        # every environment. It costs a second attempt on a transient failure,
        # which is the intended trade -- the anonymous per-IP budget is a
        # different bucket from the per-token one, so it can still succeed
        # after an authenticated 429. A genuinely bad ref fails either way.
        if ! printf 'Authorization: Bearer %s\n' "$AUTH_TOKEN" | _fetch_api_yaml -H @-; then
            echo "  Authenticated download failed; retrying anonymously (is the ambient token stale?)..." >&2
            _fetch_api_yaml
        fi
    else
        # Say so: without this line a dropped `GITHUB_TOKEN:` in the workflow
        # step silently reverts CI to the anonymous path #1205 exists to leave,
        # and the regression presents as the original intermittent 429.
        echo "  No GH_TOKEN/GITHUB_TOKEN set: downloading anonymously (shared per-IP rate budget)." >&2
        _fetch_api_yaml
    fi
    echo "Downloaded to $API_YAML"
fi

if [[ "$DOWNLOAD_ONLY" == 1 ]]; then
    echo "--download-only: stopping before model generation."
    exit 0
fi

# Ensure output directory exists
mkdir -p "$(dirname "$OUTPUT")"

# Locate tools: prefer venv, fall back to PATH
CODEGEN="${PROJECT_DIR}/.venv/bin/datamodel-codegen"
if [[ ! -x "$CODEGEN" ]]; then
    CODEGEN="$(command -v datamodel-codegen 2>/dev/null || true)"
    if [[ -z "$CODEGEN" ]]; then
        echo "Error: datamodel-codegen not found. Install with: uv pip install 'datamodel-code-generator[http]'" >&2
        exit 1
    fi
fi

RUFF="${PROJECT_DIR}/.venv/bin/ruff"
if [[ ! -x "$RUFF" ]]; then
    RUFF="$(command -v ruff 2>/dev/null || true)"
    if [[ -z "$RUFF" ]]; then
        echo "Error: ruff not found. Install with: uv pip install ruff" >&2
        exit 1
    fi
fi

# Generate models
echo "Generating Python models..."
"$CODEGEN" \
    --input "$API_YAML" \
    --input-file-type openapi \
    --output "$OUTPUT" \
    --output-model-type pydantic_v2.BaseModel \
    --target-python-version 3.12 \
    --use-standard-collections \
    --use-union-operator \
    --strict-nullable \
    --disable-timestamp \
    --use-schema-description \
    --custom-file-header "# Generated from wxyc-shared/api.yaml -- do not edit manually.
# Regenerate with: bash scripts/generate_api_models.sh"

# The WXYC/wxyc-shared#428 pin (LML#1299), ported from that repo's canonical
# scripts/generate-python-models.sh. The canonical rationale lives there and is
# NOT restated here -- read it before changing either copy. The LML-specific
# stake: `format: uri` makes pydantic validate at DECODE time, so an unpinned
# regen turns every malformed streaming_links row already in this service's
# database into a hard read failure. Three `format: uri` fields are deliberately
# out of scope (the archive presigned-GET `url`, the OAuth device-flow
# `verification_uri`/`verification_uri_complete`): they carry no stored data and
# #428's decision does not cover them, which is why this is a name-keyed
# substitution over the five and not a codegen-wide type override -- both
# --field-constraints and --strict-types are file-wide once any override targets
# `format: uri`.
#
# Ported rather than migrated onto the canonical script: that migration replaces
# a ~10KB fork wholesale (LML's copy has its own download/auth branch, LML#1205,
# and a known divergence in WXYC/wxyc-shared#369) and is a refactor with its own
# review surface. This PR has to unblock Codegen Freshness; the two shouldn't
# ride together. Migration stays available as a follow-up.
pin_streaming_url_fields_to_str() {
    local field
    for field in "${STREAMING_URL_FIELDS[@]}"; do
        sed -E "s/^([[:space:]]*${field}: )AnyUrl/\1str/" "$OUTPUT" > "$OUTPUT.pin-tmp"
        mv "$OUTPUT.pin-tmp" "$OUTPUT"
    done
}

# The substitution above is line-anchored on `fieldname: AnyUrl`, which is only
# ONE shape the generator can emit -- `Annotated[AnyUrl | None, Field(...)]`
# defeats it and every substitution silently no-ops while returning 0. That is
# the pin failing OPEN, so the post-condition cannot itself be another anchored
# regex: it parses the output and asks whether AnyUrl appears ANYWHERE in each
# pinned field's annotation, which covers shapes nobody has anticipated yet.
# tests/unit/test_generate_api_models_pin.py drives both failure modes.
verify_streaming_url_fields_pinned() {
    local python_cmd=()
    if command -v python3 &> /dev/null; then
        python_cmd=(python3)
    elif command -v python &> /dev/null; then
        python_cmd=(python)
    elif command -v uv &> /dev/null; then
        python_cmd=(uv run --no-project --python 3.12 python3)
    else
        echo "Error: no Python interpreter found to verify the #428 pin (need python3, python, or uv)." >&2
        exit 1
    fi

    STREAMING_URL_FIELDS_CSV="$(IFS=,; echo "${STREAMING_URL_FIELDS[*]}")" OUTPUT="$OUTPUT" \
        "${python_cmd[@]}" -c '
import ast
import os
import sys

output_path = os.environ["OUTPUT"]
fields = set(os.environ["STREAMING_URL_FIELDS_CSV"].split(","))

with open(output_path, "r", encoding="utf-8") as f:
    source = f.read()

tree = ast.parse(source, filename=output_path)
offenders = []
for node in ast.walk(tree):
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        if node.target.id in fields:
            annotation_src = ast.unparse(node.annotation)
            if "AnyUrl" in annotation_src:
                offenders.append(f"{node.target.id} (line {node.lineno}): {annotation_src}")

if offenders:
    sys.stderr.write(
        "#428 pin did not apply -- the following streaming URL fields still "
        "carry AnyUrl in their generated annotation (pin_streaming_url_fields_to_str "
        "silently no-op'\''d, most likely because the generator emitted a shape its "
        "sed pattern does not match):\n"
    )
    for offender in offenders:
        sys.stderr.write(f"  - {offender}\n")
    sys.exit(1)
'
}

echo "Pinning streaming URL fields to str (#428)..."
pin_streaming_url_fields_to_str
verify_streaming_url_fields_pinned

# Format with ruff. Both steps are load-bearing: byte-equality against this
# output is the entire drift contract the Codegen Freshness job checks, so a
# formatter that silently no-ops would surface one step later as a phantom
# api.yaml drift failure. Let them fail loudly instead.
echo "Formatting generated code..."
"$RUFF" format "$OUTPUT"
"$RUFF" check --fix "$OUTPUT"

echo "Generated: $OUTPUT"
