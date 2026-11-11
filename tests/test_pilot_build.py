"""Offline integration of the original reviewed artifacts, not a rebuilt wheel."""
import importlib.util
import json
import os
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pilot_build", ROOT / "deploy/a2a/build_pilot.py")


@pytest.fixture
def original_inputs():
    directory = os.environ.get("AERYS_PILOT_INPUTS")
    if not directory:
        pytest.skip("set AERYS_PILOT_INPUTS to original t_c776ba93 attachments")
    assert directory is not None
    return Path(directory)


@pytest.fixture
def builder():
    assert SPEC is not None and SPEC.loader is not None
    module = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(module)
    return module


def test_build_preserves_original_bytes_and_source_identity(tmp_path, original_inputs, builder):
    attachments = original_inputs
    output = tmp_path / "pilot"
    result = builder.build(ROOT, attachments, output)
    assert result["runtime_source_commit"] == "42e7abb5643af9a1152fa541c9147a974249d3b9"
    assert result["deployment_authorized"] is False
    assert result["wheel_source_modules_verified"] == 88
    assert len(result["artifacts"]) == 6
    assert json.loads((output / "build-metadata.json").read_text()) == result
    for artifact in result["artifacts"]:
        assert (output / artifact["name"]).read_bytes() == (
            attachments / artifact["name"] if artifact["origin"] == "parent-attachment"
            else ROOT / artifact["source_path"]
        ).read_bytes()


@pytest.mark.parametrize("name", ["aerys_v2-0.1.0-py3-none-any.whl", "requirements.lock", "aerys-runtime-manifest.json"])
def test_tampered_parent_artifact_refused_before_output(tmp_path, original_inputs, builder, name):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for original in ("aerys_v2-0.1.0-py3-none-any.whl", "requirements.lock", "aerys-runtime-manifest.json"):
        data = (original_inputs / original).read_bytes()
        (inputs / original).write_bytes(data + b"tampered" if original == name else data)
    output = tmp_path / "pilot"
    with pytest.raises(ValueError, match="digest mismatch"):
        builder.build(ROOT, inputs, output)
    assert not output.exists()


def test_existing_output_is_never_overwritten(tmp_path, original_inputs, builder):
    output = tmp_path / "pilot"
    output.mkdir()
    sentinel = output / "do-not-replace"
    sentinel.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        builder.build(ROOT, original_inputs, output)
    assert list(output.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"existing"
