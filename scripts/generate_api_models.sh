#!/usr/bin/env bash
# Generate Python Pydantic v2 models from the wxyc-shared api.yaml OpenAPI spec.
#
# Looks for api.yaml in a sibling wxyc-shared directory first, then falls back
# to downloading from GitHub. The generated file is committed to git so that
# normal CI jobs don't need the codegen toolchain.
#
# Usage:
#   bash scripts/generate_api_models.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT="$PROJECT_DIR/generated/api_models.py"

# Resolve api.yaml source
SIBLING_PATH="$PROJECT_DIR/../wxyc-shared/api.yaml"
if [[ "$PROJECT_DIR" == */.claude/worktrees/* ]]; then
    # Inside a worktree — look at the real repo's sibling
    REPO_ROOT="$(cd "$PROJECT_DIR" && git rev-parse --show-toplevel 2>/dev/null || echo "$PROJECT_DIR")"
    SIBLING_PATH="$REPO_ROOT/../wxyc-shared/api.yaml"
fi

if [[ -f "$SIBLING_PATH" ]]; then
    API_YAML="$SIBLING_PATH"
    echo "Using local api.yaml: $API_YAML"
else
    API_YAML="$(mktemp)"
    trap 'rm -f "$API_YAML"' EXIT
    echo "Downloading api.yaml from GitHub..."
    curl -sSfL "https://raw.githubusercontent.com/WXYC/wxyc-shared/main/api.yaml" -o "$API_YAML"
    echo "Downloaded to $API_YAML"
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
    RUFF="$(command -v ruff 2>/dev/null || echo ruff)"
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
    --disable-timestamp \
    --custom-file-header "# Generated from wxyc-shared/api.yaml -- do not edit manually.
# Regenerate with: bash scripts/generate_api_models.sh"

# Format with ruff
echo "Formatting generated code..."
"$RUFF" format "$OUTPUT" 2>/dev/null || true
"$RUFF" check --fix "$OUTPUT" 2>/dev/null || true

echo "Generated: $OUTPUT"
