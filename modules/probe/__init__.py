"""P4-pre: the minimum-size real-money execution probe (spec 2.15,
docs/decisions/2026-10-26-p4-prova-esecuzione-minima-piano.md).

A separate process (``python -m modules.probe``), a separate book (``probe``), one product, a few euros. It validates
the execution plant (post-only entry, standalone bracket, real fees and fills, the ``user`` channel, reconciliation, the
balance feed, the watchdog and the kill switch). It is not a strategy and not R0.

``limits.py`` holds the HARD limits as constants. The configuration can only LOWER them (``ExecProbeConfig``).
``runner.py`` is the one module, besides ``execution/gateway.py``, that builds the live gateway and a writable exchange.
"""
