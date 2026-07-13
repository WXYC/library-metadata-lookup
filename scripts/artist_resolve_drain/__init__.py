"""Bulk artist-resolve drain (LML#759 PR D).

Drives prod `POST /api/v1/artists/resolve/bulk` over the Backend-Service name set
(BS#1614) with crash-safe JSONL resume, bounded `escalation_unavailable` retries,
and a yield report + wrong-mint spot-check gating the `--live` run. Loop logic in
:mod:`.drain`; report/spot-check in :mod:`.report`; CLI in ``__main__``.
"""
