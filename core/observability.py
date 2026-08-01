"""Shared helpers for the "observability must not break the request path" idiom.

Roughly 30 sites across this repo follow the same shape: attempt a bit of
Sentry/telemetry work, and if the SDK (or an unexpected input shape) raises,
log a WARNING and let the request continue rather than propagate the
exception. This module gives that idiom composable primitives instead of a
hand-rolled ``try/except`` at each site:

- :func:`observability_guard` -- the ``try/except`` itself, as a context
  manager (also usable as a decorator -- ``contextlib.contextmanager``
  results implement ``ContextDecorator``). Swallows any ``Exception`` and
  logs ``logger.warning("Failed to %s: %s", label, e)``. That exact message
  shape is asserted by several existing tests across the call sites this
  replaces, so it is preserved verbatim rather than reworded.
- :func:`project_transaction` -- attach a dict of already-computed fields
  onto the active Sentry transaction via ``set_data`` (and, opt-in, matching
  ``set_measurement`` calls for numeric values). No-ops when there is no
  active transaction. Mirrors the bodies of ``core.search._log_hard_cap_fired``
  / ``_log_search_budget_exceeded``.
- :func:`project_capped` -- the "in-flight cap engaged" tag+measurement pair
  duplicated (with small per-site variations) across ``lookup/router.py``,
  ``streaming/router.py``, and ``core/bulk_concurrency.py``.

Neither ``project_transaction`` nor ``project_capped`` swallows exceptions on
its own -- compose them with :func:`observability_guard` at the call site.
Keeping the try/except separate from the Sentry calls means a call site with
extra bookkeeping around the projection (e.g. ``core.bulk_concurrency``'s
per-transaction running-max de-dup) can still share the primitive without the
guard hiding exceptions raised by that bookkeeping itself.

Layering: this module sits below ``lookup/`` (see the layering comment atop
``core/search.py``) -- it must not import from ``lookup/`` or anything that
does.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import sentry_sdk


@contextlib.contextmanager
def observability_guard(label: str, logger: logging.Logger):
    """Swallow any exception raised inside the block, logging a WARNING.

    ``label`` describes the attempted action in a form that reads naturally
    after "Failed to" -- e.g. ``observability_guard("record LML flag tags
    into cache_stats", logger)`` logs ``"Failed to record LML flag tags into
    cache_stats: <exception>"`` on failure. The message is built from two
    separate ``%s`` args (``logger.warning("Failed to %s: %s", label, e)``),
    not an f-string -- several existing tests assert on that exact shape.

    Usable both as a context manager (``with observability_guard(...):``)
    and, since ``contextlib.contextmanager`` results are ``ContextDecorator``s,
    as a decorator on a synchronous function.

    Only ``Exception`` is caught -- a ``KeyboardInterrupt``/``SystemExit``
    still propagates, matching every site this replaces (a bare
    ``except Exception`` never catches those).
    """
    try:
        yield
    except Exception as e:
        logger.warning("Failed to %s: %s", label, e)


def project_transaction(
    data: dict[str, Any],
    *,
    measurements: bool = False,
    prefix: str = "",
) -> None:
    """Attach ``data`` onto the active Sentry transaction via ``set_data``.

    No-op when there is no active transaction (Sentry not initialized, or
    called outside a request span) -- mirrors every site this replaces.

    Each key is prefixed with ``prefix`` before being set, e.g.
    ``prefix="lml.cache."`` turns ``"api_calls"`` into ``"lml.cache.api_calls"``.

    When ``measurements`` is True, numeric entries (``int``/``float``,
    excluding ``bool`` -- a ``bool`` is an ``int`` subclass in Python) are
    ALSO recorded via ``set_measurement``, so they aggregate in Sentry's
    metrics/alerting surface (``set_data`` alone reads back as "Unknown
    attribute" there -- the LML#683 lesson). Every entry still gets
    ``set_data`` regardless of type; ``measurements`` only controls whether
    numeric entries additionally get ``set_measurement``.

    Does not swallow exceptions -- wrap the call site in
    :func:`observability_guard` for that.
    """
    transaction = sentry_sdk.get_current_scope().transaction
    if transaction is None:
        return
    for key, value in data.items():
        full_key = f"{prefix}{key}"
        transaction.set_data(full_key, value)
        if measurements and isinstance(value, (int, float)) and not isinstance(value, bool):
            transaction.set_measurement(full_key, value)


def project_capped(
    tag_key: str,
    measurement_key: str,
    wait_ms: float,
    *,
    also_set_data: bool = True,
) -> None:
    """Project "this request queued on a capacity gate" onto Sentry.

    Two channels, per the LML#683 lesson (``set_data`` alone is unqueryable
    in the spans/metrics dataset):

    * ``sentry_sdk.set_tag(tag_key, "true")`` -- the filterable engagement
      flag. Call sites only invoke this when the gate was found saturated on
      arrival, so uncontended requests stay untagged. Set unconditionally
      (not gated on an active transaction existing).
    * ``transaction.set_measurement(measurement_key, wait_ms)`` -- the
      quantitative queue-wait series used to tune the gate's cap. Only set
      when there is an active transaction. When ``also_set_data`` is True
      (the default), a matching ``transaction.set_data(measurement_key,
      wait_ms)`` is also recorded, mirroring the two call sites
      (``lookup.router``, ``core.bulk_concurrency``) that duplicate the value
      onto both channels; ``streaming.router``'s call site passes
      ``also_set_data=False`` since its original body never called
      ``set_data``.

    Does not swallow exceptions -- wrap the call site in
    :func:`observability_guard`.
    """
    sentry_sdk.set_tag(tag_key, "true")
    transaction = sentry_sdk.get_current_scope().transaction
    if transaction is not None:
        transaction.set_measurement(measurement_key, wait_ms)
        if also_set_data:
            transaction.set_data(measurement_key, wait_ms)
