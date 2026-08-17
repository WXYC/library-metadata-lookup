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
# When GH_TOKEN or GITHUB_TOKEN is set (GH_TOKEN wins, matching gh's own
# precedence -- `gh help environment`), the GitHub download authenticates via a
# Bearer Authorization header: anonymous raw.githubusercontent.com requests
# share a per-IP rate budget across the Actions runner pool and intermittently
# 429 (#1205), while authenticated ones get a per-token budget. The header
# rides curl's stdin, so the token value never reaches argv (visible in `ps`)
# or the log, and both variables are unset after capture so the codegen
# toolchain never sees them. A failed authenticated attempt retries once
# anonymously -- GitHub 404s (not 401s) raw requests carrying a stale token, so
# without the fallback an expired ambient token would break previously-working
# local runs. Unset-token runs are unchanged.
#
# --download-only stops after resolving/fetching api.yaml, skipping codegen and
# formatting. It exists for the unit tests covering the download branch
# (tests/unit/test_generate_api_models_auth.py).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT="$PROJECT_DIR/generated/api_models.py"

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
    # Ambient GitHub token (#1205): authenticated raw downloads get a per-token
    # rate budget instead of the Actions runner pool's shared per-IP one, which
    # intermittently 429s. GH_TOKEN is preferred over GITHUB_TOKEN, matching
    # gh's own precedence (`gh help environment`). Captured into a shell
    # variable and immediately dropped from the environment: only the download
    # needs it, so the codegen/ruff child processes never inherit it.
    AUTH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
    unset GITHUB_TOKEN GH_TOKEN
    # --max-time/--retry mirror wxyc-shared's generate-python-models.sh pin for
    # this same download; curl's --retry also covers 429/5xx, honoring
    # Retry-After, which directly serves the #1205 goal.
    CURL_OPTS=(-sSfL --max-time 30 --retry 3)
    if [[ -n "$AUTH_TOKEN" ]]; then
        echo "  Authenticated download: sending Authorization header (token value not logged)." >&2
        # -H @- reads the header from stdin, keeping the token off the process
        # argv (visible in `ps` on shared hosts). A stale/revoked token makes
        # raw.githubusercontent.com return 404 -- not 401 -- with no anonymous
        # fallback server-side, so a failed authenticated attempt retries once
        # anonymously: pre-#1205 anonymous behavior is the floor in every
        # environment, and a genuinely bad ref still fails (the anonymous
        # retry 404s too).
        if ! printf 'Authorization: Bearer %s\n' "$AUTH_TOKEN" \
            | curl "${CURL_OPTS[@]}" -H @- "$API_YAML_URL" -o "$API_YAML"; then
            echo "  Authenticated download failed; retrying anonymously (is the ambient token stale?)..." >&2
            curl "${CURL_OPTS[@]}" "$API_YAML_URL" -o "$API_YAML"
        fi
    else
        curl "${CURL_OPTS[@]}" "$API_YAML_URL" -o "$API_YAML"
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

# Format with ruff. Both steps are load-bearing: byte-equality against this
# output is the entire drift contract the Codegen Freshness job checks, so a
# formatter that silently no-ops would surface one step later as a phantom
# api.yaml drift failure. Let them fail loudly instead.
echo "Formatting generated code..."
"$RUFF" format "$OUTPUT"
"$RUFF" check --fix "$OUTPUT"

echo "Generated: $OUTPUT"
