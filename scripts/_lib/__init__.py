"""Shared helpers for the one-shot scripts under ``scripts/``.

Extracted to remove cross-script duplication of the SIGINT/SIGTERM
graceful-shutdown handler (audit finding #7) and the ``main()`` runtime
preamble (audit finding #8). See ``signals.py`` and ``runtime.py``.
"""
