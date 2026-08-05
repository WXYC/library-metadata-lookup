# Plan documents

`docs/plans/` is the single home for this repo's design/implementation plan documents. The top-level `plans/` directory that used to hold a second, untracked copy of the same kind of document no longer exists — LML#1124 moved every file out of it and into here, keeping basenames unchanged.

## Citation convention

Every plan citation appearing in a code span, comment, or docstring — the kind of reference `config/settings.py` or `entrypoint.sh` makes when explaining why a tuning knob has the value it does — is repo-root-relative: `docs/plans/<name>.md`. This is the one spelling used everywhere in tracked code, tests, and other plan documents, and it is what `scripts/check_plan_links.sh` enforces in CI: the checker only recognizes citations that contain a literal `plans/` segment, so a citation rewritten to a bare filename (dropping the segment entirely) would silently stop being checked rather than fail loudly.

**This is enforced by CI only for citations matching that pattern.** The checker's fallback resolution (`tracked "$dir/$path" || tracked "$path"`) means that for any file living under `docs/`, both `docs/plans/<name>.md` and the docs-relative `plans/<name>.md` happen to resolve — the fallback checks the citing file's own directory first, and `docs/` is exactly one level above `docs/plans/`. So the one-spelling rule is enforced everywhere outside `docs/`, but only advisory for files inside `docs/`: CI will not catch a new docs-relative citation added to a file under `docs/`. Keep using `docs/plans/…` there anyway, for consistency with everything else.

## Rendered markdown links are the one exception

Citations above are prose references inside code spans, comments, or docstrings — never resolved by anything but the checker. An actual rendered markdown link (`[text](path)`) is different: GitHub resolves it relative to the directory of the file containing it, not the repo root, so a link written as `docs/plans/<name>.md` from a file already under `docs/` would 404.

Exactly one such link exists today: `docs/scripts.md`'s entry for the per-consumer API keys plan links to this directory's `lml-per-consumer-api-keys.md` using a path relative to `docs/`, not the repo-root-relative form. That link stays docs-relative deliberately and is the sole documented exception to the convention above. If you add a new rendered link to a plan document from somewhere under `docs/`, keep it docs-relative for the same reason; if you add one from outside `docs/` (or as a citation in code, a comment, or a docstring anywhere), use the repo-root-relative `docs/plans/…` form.

## Related directories

`docs/adr/` holds architecture decision records; `docs/reviews/` holds point-in-time code review write-ups. Plans, ADRs, and reviews are different kinds of documents and live in separate directories for that reason, even though all three are prose that outlives the PR that produced it.
