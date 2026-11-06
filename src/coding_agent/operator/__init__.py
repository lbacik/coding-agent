"""The operator contract's in-process side (adoption step 4, #245).

Contract: the operator store lives in the Run Ledger's SQLite file with
separate tables and its own connection; the control listener is an
in-process thread on a Unix socket that serves ``identity`` and a minimal
``status`` from the prepared live view and its own store/ledger reads.
"""

from __future__ import annotations
