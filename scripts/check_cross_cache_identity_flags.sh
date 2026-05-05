#!/usr/bin/env bash
# Asserts that the LML-owned cross-cache-identity feature flags listed in
# CLAUDE.md's "Cross-cache-identity feature flags" subsection match the §4.2
# locked inventory.
#
# Tier 1 (this PR, BS#667): doc-vs-expected-list.
# Tier 2 (E2-LML cascade PR): adds a check that flag names referenced in code
# match the doc; this script will be extended at that time.
#
# Plan reference: WXYC/wiki plans/library-hook-canonicalization-plan.md §4.2.
# Canonical: WXYC/Backend-Service/CLAUDE.md "Cross-cache-identity feature flags (canonical inventory)".

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLAUDE_MD="$REPO_ROOT/CLAUDE.md"

if [[ ! -f "$CLAUDE_MD" ]]; then
  echo "FAIL: $CLAUDE_MD not found." >&2
  exit 1
fi

# Locked LML-owned flag names per §4.2.
expected=(
  "LML_USE_NEW_HOOK_DISCOGS"
  "LML_USE_NEW_HOOK_DISCOGS_FULL"
  "LML_USE_NEW_HOOK_MUSICBRAINZ"
  "LML_USE_NEW_HOOK_WIKIDATA"
  "LML_MANUAL_OVERRIDE_CHECK_DISABLED"
)

# Extract LML_* flag names from the "### Cross-cache-identity feature flags"
# subsection (until the next ### header or EOF).
documented=$(
  awk '
    /^### Cross-cache-identity feature flags/ { in_section = 1; next }
    /^### / && in_section { in_section = 0 }
    in_section { print }
  ' "$CLAUDE_MD" | grep -oE '`(LML_[A-Z_]+)`' | tr -d '`' | sort -u
)

if [[ -z "$documented" ]]; then
  echo "FAIL: no LML_* flags found under the '### Cross-cache-identity feature flags' subsection in CLAUDE.md." >&2
  exit 1
fi

expected_sorted=$(printf '%s\n' "${expected[@]}" | sort -u)

missing=$(comm -23 <(printf '%s\n' "$expected_sorted") <(printf '%s\n' "$documented"))
extra=$(comm -13 <(printf '%s\n' "$expected_sorted") <(printf '%s\n' "$documented"))

failed=0
if [[ -n "$missing" ]]; then
  echo "FAIL: §4.2 LML-owned flags missing from CLAUDE.md:" >&2
  echo "$missing" | sed 's/^/  - /' >&2
  failed=1
fi
if [[ -n "$extra" ]]; then
  echo "FAIL: CLAUDE.md lists LML_* flags not in §4.2:" >&2
  echo "$extra" | sed 's/^/  - /' >&2
  echo "  If a new LML cross-cache-identity flag is being introduced, update §4.2 first." >&2
  failed=1
fi

if [[ "$failed" -ne 0 ]]; then
  echo "" >&2
  echo "Canonical inventory: WXYC/Backend-Service/CLAUDE.md 'Cross-cache-identity feature flags (canonical inventory)'." >&2
  exit 1
fi

count=$(printf '%s\n' "$expected_sorted" | wc -l | tr -d ' ')
echo "PASS: $count LML-owned cross-cache-identity flag(s) consistent with §4.2."
