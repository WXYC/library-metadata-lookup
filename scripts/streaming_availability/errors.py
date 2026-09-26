"""Errors the streaming-availability writers raise, where a caller must discriminate.

``update_result``'s refusal of a mis-addressed service and an ordinary transient
API condition both arrived as a bare ``ValueError``, so a caller could only catch
both or neither. The compilation drain wants to re-raise the first -- a typo in
its lane table is a programming error, and returning 0 rows instead made it a
silent no-op that counted every album as a clean miss -- while tolerating the
second: ``SpotifyClient.search_album`` raises ``ValueError`` from ``int()`` on a
legal date-form ``Retry-After`` and from ``resp.json()``
(``json.JSONDecodeError`` is a ``ValueError`` subclass) on a non-JSON 200 body,
and one 429 in hour three must not end a multi-hour population run.

Subclasses ``ValueError`` on purpose: both ``update_result`` docstrings document
``Raises: ValueError`` and existing callers and tests catch it under that name,
so narrowing the type stays backward compatible.

Scope is service *addressing* only -- an unroutable token, or a service routed to
the wrong method. Guards keyed on something other than the service (a refused
``status`` value, a rate argument) stay bare ``ValueError``: widening this to
"every programming error in the package" would put it back to meaning nothing in
particular at a catch site.
"""

from __future__ import annotations


class StreamingServiceRoutingError(ValueError):
    """A service token this table cannot route: unknown, or wrong method for it."""
