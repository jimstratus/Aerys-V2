"""Stage original reviewed Aerys inputs locally; never install or deploy them."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import zipfile

SOURCE = "42e7abb5643af9a1152fa541c9147a974249d3b9"
MANIFEST_SHA256 = "89035b692137818663ce0077051ed3f949198daf56105c29d98503069b3e8145"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def build(root: Path, inputs: Path, output: Path) -> dict:
    """Validate everything before creating a new, nonexisting output directory."""
    lock = json.loads((root / "deploy/a2a/pilot-source-lock.json").read_text())
    if lock["aerys"]["runtime_source_commit"] != SOURCE:
        raise ValueError("runtime source pin mismatch")
    manifest_bytes = (inputs / "aerys-runtime-manifest.json").read_bytes()
    if digest(manifest_bytes) != MANIFEST_SHA256:
        raise ValueError("parent manifest digest mismatch")
    manifest = json.loads(manifest_bytes)
    if manifest["source_commit"] != SOURCE:
        raise ValueError("parent manifest source mismatch")
    artifacts = []
    payloads = {}
    for name, origin, path in [
        (lock["aerys"]["wheel"], "parent-attachment", lock["aerys"]["wheel"]),
        ("requirements.lock", "parent-attachment", "requirements.lock"),
        ("aerys-runtime-manifest.json", "parent-attachment", "aerys-runtime-manifest.json"),
        ("uv.lock", "pinned-git-tree", "uv.lock"),
        ("aerys-a2a.service", "pinned-git-tree", "deploy/a2a/aerys-a2a.service"),
        ("config.example.json", "pinned-git-tree", "deploy/a2a/config.example.json"),
    ]:
        data = ((inputs if origin == "parent-attachment" else root) / path).read_bytes()
        expected = MANIFEST_SHA256 if name == "aerys-runtime-manifest.json" else manifest["artifacts"][name]["sha256"]
        if digest(data) != expected:
            raise ValueError(f"artifact digest mismatch: {name}")
        if origin == "pinned-git-tree" and data != git(root, "show", f"{SOURCE}:{path}"):
            raise ValueError(f"artifact source mismatch: {name}")
        payloads[name] = data
        artifacts.append({"name": name, "sha256": expected, "bytes": len(data),
                          "origin": origin, "source_path": path,
                          "repository": "jimstratus/Aerys-V2", "source_commit": SOURCE,
                          "owner_task": "t_c776ba93"})
    if digest(payloads[lock["aerys"]["wheel"]]) != lock["aerys"]["wheel_sha256"]:
        raise ValueError("wheel lock mismatch")
    if digest(payloads["requirements.lock"]) != lock["aerys"]["requirements_sha256"]:
        raise ValueError("requirements lock mismatch")
    with zipfile.ZipFile(io.BytesIO(payloads[lock["aerys"]["wheel"]])) as wheel:
        names = sorted(n for n in wheel.namelist() if n.startswith("aerys_v2/") and n.endswith(".py"))
        tracked = git(root, "ls-tree", "-r", "--name-only", SOURCE, "src/aerys_v2").decode().splitlines()
        expected_names = sorted(n.removeprefix("src/") for n in tracked if n.endswith(".py"))
        if names != expected_names:
            raise ValueError("wheel/source module inventory mismatch")
        for name in names:
            data = wheel.read(name)
            if data != git(root, "show", f"{SOURCE}:src/{name}") or data != (root / "src" / name).read_bytes():
                raise ValueError(f"wheel/source module mismatch: {name}")
    metadata = {
        "schema_version": 1, "deployment_authorized": False,
        "runtime_source_commit": SOURCE,
        "integration_commit": git(root, "rev-parse", "HEAD").decode().strip(),
        "integration_worktree_dirty": bool(git(root, "status", "--porcelain", "--untracked-files=normal")),
        "source_lock_sha256": digest((root / "deploy/a2a/pilot-source-lock.json").read_bytes()),
        "builder_sha256": digest(Path(__file__).read_bytes()),
        "wheel_source_modules_verified": len(names), "artifacts": artifacts,
    }
    output.mkdir(parents=True, exist_ok=False)
    for name, data in payloads.items():
        (output / name).write_bytes(data)
    (output / "build-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True, help="Original t_c776ba93 attachments directory")
    parser.add_argument("--output", type=Path, required=True, help="New local artifact directory (must not exist)")
    args = parser.parse_args()
    result = build(Path(__file__).resolve().parents[2], args.inputs, args.output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
