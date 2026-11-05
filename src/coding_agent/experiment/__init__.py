"""The experiment harness (ADR 0019): interleaved paid Attempts, per-run evidence.

Runs outside ``agent run``: only this package (and the runtime seam it
exercises) constructs the experiment-only ``silence_ends_stage`` step exit
policy. The ``agent run`` entry path and Workflow Configuration loading never
import this package.
"""
