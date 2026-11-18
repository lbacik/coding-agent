"""Load a Workflow Configuration through the single public entry point.

Tests go through the interface: YAML in, faults out. ``load_config`` wraps
``load_workflow_configuration`` and unwraps the loaded value, returning the
``(config | None, faults)`` pair validation tests assert on.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from coding_agent.workflow.configuration import load_workflow_configuration
from coding_agent.workflow.parser import WorkflowConfigError
from coding_agent.workflow.schema import V3Config


def load_config(
    config_path: Path | str,
    workflow_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[V3Config | None, list[WorkflowConfigError]]:
    """Load and validate ``config_path``; ``(config, faults)``.

    On success ``(config, [])``; on failure ``(None, faults)``. A file that
    is not valid UTF-8 raises :class:`UnicodeDecodeError`, mirroring the
    entry point.
    """
    loaded, faults = load_workflow_configuration(config_path, workflow_id, env=env)
    if loaded is None:
        return None, faults
    return loaded.config, []
