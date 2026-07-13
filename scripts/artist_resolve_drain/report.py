"""Yield report + wrong-mint spot-check for the artist-resolve drain (LML#759 PR D).

`build_report` reduces the drain's JSONL records (mode-scoped, one verdict per
name after `latest_by_name`) to the distributions BS#1614 asked for: per-method
yield, cache-corroboration + candidate-count distributions, ambiguity rate,
alias-leg-only and trigram-near-miss counts (v2 alias-arm sizing), and the
`escalation_unavailable` residual. `sample_spot_check` draws a deterministic
random sample of the `api_search` mints — the newly-verified rows carrying the
accepted wrong-mint risk — with `discogs.com/artist/<id>` links for the human
gate before `--live`. `format_report_markdown` renders both for pasting into the
issue.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Sequence
from typing import Any

from scripts.artist_resolve_drain.drain import latest_by_name

# Alias-shaped cache legs — matches that can't be globally uniqueness-checked in
# v1 but size a possible v2 alias arm (LML#759 design).
ALIAS_FAMILY_LEGS = ("cache_alias", "cache_member", "cache_name_variation")

DISCOGS_ARTIST_URL = "https://www.discogs.com/artist/{id}"


def _is_resolved(rec: dict[str, Any]) -> bool:
    return rec.get("discogs_artist_id") is not None


def _candidate_bucket(count: int | None) -> str:
    if count is None:
        return "null"
    if count >= 3:
        return "3+"
    return str(count)


def build_report(
    records: Sequence[dict[str, Any]],
    all_names: Sequence[str],
    max_attempts: int,
) -> dict[str, Any]:
    """Aggregate the drain records into the BS#1614 yield report.

    ``records`` should be scoped to the reporting mode; ``all_names`` is the full
    requested set (so names never processed — e.g. a shutdown mid-drain — show up
    as ``not_processed`` rather than silently vanishing).
    """
    latest = latest_by_name(records)
    unique_names = list(dict.fromkeys(all_names))

    resolved_by_method: Counter[str] = Counter()
    corr_resolved: Counter[str] = Counter()
    corr_not_found: Counter[str] = Counter()
    candidate_dist: Counter[str] = Counter()

    resolved = not_found = ambiguous = escalation = not_processed = measured = 0
    ambiguous_measured = 0
    alias_only = trigram_near = 0

    for name in unique_names:
        rec = latest.get(name)
        if rec is None:
            not_processed += 1
            continue
        is_measured = rec.get("candidate_count") is not None
        if is_measured:
            measured += 1
        candidate_dist[_candidate_bucket(rec.get("candidate_count"))] += 1
        corr = rec.get("cache_corroboration") or []
        if _is_resolved(rec):
            resolved += 1
            method = rec.get("method")
            if method is not None:
                resolved_by_method[method] += 1
            for leg in corr:
                corr_resolved[leg] += 1
            continue
        reason = rec.get("unresolved_reason")
        if reason == "not_found":
            not_found += 1
            for leg in corr:
                corr_not_found[leg] += 1
            # Alias-leg-*only*: the alias family is the sole corroboration, so
            # this name would be recoverable only via a v2 alias arm. A not_found
            # that also carries an equality/trigram leg is not alias-only.
            if corr and all(leg in ALIAS_FAMILY_LEGS for leg in corr):
                alias_only += 1
            if "cache_trigram" in corr:
                trigram_near += 1
        elif reason == "ambiguous":
            ambiguous += 1
            # Store-conflict ambiguity carries candidate_count=None (doubt without
            # a measurement); only measured ambiguity belongs in the rate's base.
            if is_measured:
                ambiguous_measured += 1
        elif reason == "escalation_unavailable":
            escalation += 1

    return {
        "total_names": len(unique_names),
        "processed": len(unique_names) - not_processed,
        "not_processed": not_processed,
        "resolved": resolved,
        "resolved_by_method": dict(resolved_by_method),
        "not_found": not_found,
        "ambiguous": ambiguous,
        "escalation_residual": escalation,
        "measured": measured,
        "ambiguity_rate": (ambiguous_measured / measured) if measured else 0.0,
        "corroboration_resolved": dict(corr_resolved),
        "corroboration_not_found": dict(corr_not_found),
        "candidate_count_dist": dict(candidate_dist),
        "alias_only_not_found": alias_only,
        "trigram_near_miss": trigram_near,
        "max_attempts": max_attempts,
    }


def sample_spot_check(
    records: Sequence[dict[str, Any]], *, seed: int = 0, k: int = 20
) -> list[dict[str, Any]]:
    """Deterministic random sample of `api_search` mints for the human review.

    Only `api_search` resolutions are drawn — `identity_store` short-circuits are
    pre-existing rows this drain did not verify, so they carry no new wrong-mint
    risk. Sampling is seeded and drawn from a name-sorted pool so the table is
    reproducible from the JSONL.
    """
    latest = latest_by_name(records)
    pool = [
        rec for rec in latest.values() if _is_resolved(rec) and rec.get("method") == "api_search"
    ]
    pool.sort(key=lambda r: r["name"])
    sample = pool if k >= len(pool) else random.Random(seed).sample(pool, k)
    return [
        {
            "name": rec["name"],
            "canonical_name": rec.get("canonical_name"),
            "discogs_artist_id": rec["discogs_artist_id"],
            "url": DISCOGS_ARTIST_URL.format(id=rec["discogs_artist_id"]),
            "cache_corroboration": rec.get("cache_corroboration") or [],
            "candidate_count": rec.get("candidate_count"),
        }
        for rec in sample
    ]


def _fmt_counter(counter: dict[str, Any]) -> str:
    if not counter:
        return "(none)"
    return ", ".join(f"`{k}`: {v}" for k, v in sorted(counter.items()))


def format_report_markdown(
    report: dict[str, Any],
    spot_check_rows: Sequence[dict[str, Any]],
    *,
    dry_run: bool,
) -> str:
    """Render the report + spot-check table as issue-ready markdown."""
    mode = "DRY RUN" if dry_run else "LIVE"
    lines: list[str] = []
    lines.append(f"## Drain report — {mode}")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Total names | {report['total_names']} |")
    lines.append(f"| Processed | {report['processed']} |")
    if report["not_processed"]:
        lines.append(f"| Not processed | {report['not_processed']} |")
    lines.append(f"| Resolved | {report['resolved']} |")
    by_method = report["resolved_by_method"]
    lines.append(f"| &nbsp;&nbsp;via `api_search` | {by_method.get('api_search', 0)} |")
    lines.append(f"| &nbsp;&nbsp;via `identity_store` | {by_method.get('identity_store', 0)} |")
    lines.append(f"| Not found | {report['not_found']} |")
    lines.append(f"| Ambiguous | {report['ambiguous']} |")
    lines.append(f"| Ambiguity rate (of measured) | {report['ambiguity_rate']:.1%} |")
    lines.append(f"| Residual (`escalation_unavailable`) | {report['escalation_residual']} |")
    lines.append(f"| Alias-leg-only not_found (v2 sizing) | {report['alias_only_not_found']} |")
    lines.append(f"| Trigram near-miss not_found | {report['trigram_near_miss']} |")
    lines.append("")
    lines.append(
        f"**Candidate-count distribution:** {_fmt_counter(report['candidate_count_dist'])}"
    )
    lines.append("")
    lines.append(f"**Corroboration (resolved):** {_fmt_counter(report['corroboration_resolved'])}")
    lines.append(
        f"**Corroboration (not_found):** {_fmt_counter(report['corroboration_not_found'])}"
    )
    lines.append("")
    lines.append(f"### Spot-check ({len(spot_check_rows)} random `api_search` mints)")
    lines.append("")
    lines.append("Eyeball each row for a wrong mint before authorizing `--live`.")
    lines.append("")
    lines.append("| Input name | Discogs title | Artist | Corroboration | Cand. |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in spot_check_rows:
        corr = ", ".join(row["cache_corroboration"]) or "—"
        lines.append(
            f"| {row['name']} | {row['canonical_name'] or '—'} "
            f"| [{row['discogs_artist_id']}]({row['url']}) | {corr} | {row['candidate_count']} |"
        )
    lines.append("")
    return "\n".join(lines)
