"""Unit tests for ``scripts/generate-toolchain-matrix.py``.

The generator takes an explicit ``--runtime`` selection (repeatable) so a
derived deployment image records only the Target Project toolchains it
provisioned. The base image carries Python+uv (and Node) for coding-agent's
own use, so inferring supported runtimes from whatever is on PATH would
wrongly advertise Python in e.g. a PHP-only deployment's Matrix.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest


def load_generator() -> types.ModuleType:
    path = Path(__file__).resolve().parent.parent / "scripts" / "generate-toolchain-matrix.py"
    spec = importlib.util.spec_from_file_location("generate_toolchain_matrix", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def generator() -> types.ModuleType:
    return load_generator()


def _fake_environment(
    monkeypatch: pytest.MonkeyPatch,
    generator: types.ModuleType,
    *,
    present: dict[str, str],
) -> None:
    """Fake ``shutil.which``/``subprocess.run`` so `present` maps a binary
    name to its ``--version`` stdout; anything absent is treated as missing."""

    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in present else None

    def fake_run(
        command: list[str],
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert capture_output and text and check
        return subprocess.CompletedProcess(command, 0, present[command[0]], "")

    monkeypatch.setattr(generator.shutil, "which", fake_which)
    monkeypatch.setattr(generator.subprocess, "run", fake_run)


FULL_ENVIRONMENT: dict[str, str] = {
    "python3": "Python 3.13.9",
    "uv": "uv 0.5.29",
    "php": "8.4.27",
    "composer": "Composer version 2.8.9",
    "node": "v22.23.2",
    "pnpm": "9.15.4",
}


def test_explicit_php_only_omits_python_even_when_python_is_present(
    monkeypatch: pytest.MonkeyPatch, generator: types.ModuleType
) -> None:
    """A PHP-only derived image must not advertise the base image's
    Python+uv as a supported Target Project toolchain."""
    _fake_environment(monkeypatch, generator, present=dict(FULL_ENVIRONMENT))
    matrix = generator.build_matrix(["php"])
    assert set(matrix["toolchains"]) == {"php"}


def test_explicit_selection_keeps_requested_runtimes_in_canonical_order(
    monkeypatch: pytest.MonkeyPatch, generator: types.ModuleType
) -> None:
    _fake_environment(monkeypatch, generator, present=dict(FULL_ENVIRONMENT))
    matrix = generator.build_matrix(["node", "python"])
    assert set(matrix["toolchains"]) == {"python", "node"}
    assert matrix["toolchains"]["python"] == {
        "version": "3.13.9",
        "package_manager": {"name": "uv", "version": "0.5.29"},
    }


def test_explicit_selection_fails_when_the_runtime_is_absent(
    monkeypatch: pytest.MonkeyPatch, generator: types.ModuleType
) -> None:
    present = dict(FULL_ENVIRONMENT)
    del present["php"]
    _fake_environment(monkeypatch, generator, present=present)
    with pytest.raises(generator.MatrixGenerationError, match="php"):
        generator.build_matrix(["php"])


def test_explicit_selection_fails_when_the_package_manager_is_absent(
    monkeypatch: pytest.MonkeyPatch, generator: types.ModuleType
) -> None:
    present = dict(FULL_ENVIRONMENT)
    del present["composer"]
    _fake_environment(monkeypatch, generator, present=present)
    with pytest.raises(generator.MatrixGenerationError, match="composer"):
        generator.build_matrix(["php"])


def test_no_selection_records_whatever_is_present_for_local_host_use(
    monkeypatch: pytest.MonkeyPatch, generator: types.ModuleType
) -> None:
    """Running with no selection keeps the legacy auto-detect behaviour for
    local host use (README): only runtimes found on PATH are recorded."""
    _fake_environment(
        monkeypatch,
        generator,
        present={"python3": "Python 3.13.9", "uv": "uv 0.5.29"},
    )
    matrix = generator.build_matrix()
    assert set(matrix["toolchains"]) == {"python"}


def test_main_refuses_an_explicit_selection_it_cannot_satisfy(
    monkeypatch: pytest.MonkeyPatch, generator: types.ModuleType, tmp_path: Path
) -> None:
    present = dict(FULL_ENVIRONMENT)
    del present["pnpm"]
    _fake_environment(monkeypatch, generator, present=present)
    output = tmp_path / "toolchain-matrix.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["generate-toolchain-matrix.py", "--runtime", "node", "--output", str(output)],
    )
    with pytest.raises(SystemExit) as excinfo:
        generator.main()
    assert excinfo.value.code == 2
    assert not output.exists()


def test_main_writes_the_selected_matrix(
    monkeypatch: pytest.MonkeyPatch,
    generator: types.ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake_environment(monkeypatch, generator, present=dict(FULL_ENVIRONMENT))
    output = tmp_path / "toolchain-matrix.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate-toolchain-matrix.py",
            "--runtime",
            "python",
            "--runtime",
            "php",
            "--runtime",
            "node",
            "--output",
            str(output),
        ],
    )
    assert generator.main() == 0
    assert "wrote Supported Toolchain Matrix" in capsys.readouterr().out
    matrix = generator.json.loads(output.read_text(encoding="utf-8"))
    assert set(matrix["toolchains"]) == {"python", "php", "node"}


def test_build_matrix_signature_accepts_no_selection(
    generator: types.ModuleType,
) -> None:
    import inspect

    parameters = inspect.signature(generator.build_matrix).parameters
    assert "selected" in parameters
    assert parameters["selected"].default is not inspect.Parameter.empty


BuildMatrix = Callable[..., dict[str, object]]
