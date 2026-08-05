#!/usr/bin/env bash
# Every local plans/*.md citation in tracked files must resolve to a TRACKED file.
# Resolves against the git index, not the filesystem, so an untracked file on a
# developer's disk cannot mask a real break.
# Skips generated/ (codegen'd upstream), URL-form citations, and cross-repo docs.
set -uo pipefail
CROSS_REPO_DOCS="library-hook-canonicalization.md library-hook-canonicalization-plan.md rotation-discogs-unavailable.md test-patterns.md"
status=0
tracked(){ git ls-files --error-unmatch "$1" >/dev/null 2>&1; }
while IFS= read -r line; do
  file="${line%%:*}"; dir=$(dirname "$file")
  for path in $(printf '%s\n' "$line" \
      | grep -oE "[A-Za-z0-9_/.-]*plans/[A-Za-z0-9._-]+\.md" | sort -u); do
    # URL-form citation (https://github.com/OWNER/REPO/.../plans/x.md) — not a local path.
    case "$path" in *//*|*github.com*) continue;; esac
    case " $CROSS_REPO_DOCS " in *" $(basename "$path") "*) continue;; esac
    tracked "$dir/$path" || tracked "$path" || { echo "BROKEN: $file:$path"; status=1; }
  done
done < <(git ls-files -z | grep -zv '^generated/' \
           | xargs -0 grep -HnE "[A-Za-z0-9_/.-]*plans/[A-Za-z0-9._-]+\.md")
exit $status
