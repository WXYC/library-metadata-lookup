# Plan documents

`docs/plans/` is the single home for this repo's design/implementation plan documents. The top-level `plans/` directory that used to hold a second, untracked copy of the same kind of document no longer exists — LML#1124 moved every file out of it and into here, keeping basenames unchanged.

## Citation convention

Every plan citation appearing in a code span, comment, or docstring — the kind of reference `config/settings.py` or `entrypoint.sh` makes when explaining why a tuning knob has the value it does — is repo-root-relative: `docs/plans/<name>.md`. This is the one spelling used everywhere in tracked code, tests, and other plan documents, and it is what `scripts/check_plan_links.sh` enforces in CI: the checker only recognizes citations that contain a literal `plans/` segment, so a citation rewritten to a bare filename (dropping the segment entirely) would silently stop being checked rather than fail loudly.

**This is enforced by CI only for citations matching that pattern**, and there is exactly one narrow spot where the rule is advisory rather than enforced. The checker's fallback resolution (`tracked "$dir/$path" || tracked "$path"`) prepends *the citing file's own directory* before retrying bare — so from a file sitting **directly in `docs/`** (`docs/scripts.md`, `docs/env-vars.md`), the docs-relative `plans/<name>.md` also resolves, because `docs/` + `plans/<name>.md` is the real path. For those files, and only those, CI will not catch a new docs-relative citation.

Everywhere else the one-spelling rule is hard-enforced, **including the rest of the `docs/` tree**: from `docs/plans/` itself or from `docs/adr/`, a docs-relative `plans/<name>.md` resolves to neither `docs/plans/plans/<name>.md` nor a repo-root `plans/<name>.md`, so it fails CI. (That is exactly why LML#1124's plan-to-plan rewrites inside this directory were mandatory, not cosmetic.) `tests/unit/test_check_plan_links.py::test_docs_relative_citation_resolves_only_one_level_above_plans` pins this boundary. Use `docs/plans/…` everywhere regardless — the advisory gap is a quirk of the fallback, not a second sanctioned spelling.

## Rendered markdown links are the one exception

Citations above are prose references inside code spans, comments, or docstrings — never resolved by anything but the checker. An actual rendered markdown link (`[text](path)`) is different: GitHub resolves it relative to the directory of the file containing it, not the repo root, so a link written as `docs/plans/<name>.md` from a file already under `docs/` would 404.

Exactly one such link exists today: `docs/scripts.md`'s entry for the per-consumer API keys plan links to this directory's `lml-per-consumer-api-keys.md` using a path relative to `docs/`, not the repo-root-relative form. That link stays docs-relative deliberately and is the sole documented exception to the convention above. If you add a new rendered link to a plan document from somewhere under `docs/`, keep it docs-relative for the same reason; if you add one from outside `docs/` (or as a citation in code, a comment, or a docstring anywhere), use the repo-root-relative `docs/plans/…` form.

## Related directories

`docs/adr/` holds architecture decision records; `docs/reviews/` holds point-in-time code review write-ups. Plans, ADRs, and reviews are different kinds of documents and live in separate directories for that reason, even though all three are prose that outlives the PR that produced it.
