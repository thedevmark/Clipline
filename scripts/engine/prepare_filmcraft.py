"""Prepare a fresh, verified Filmcraft source tree without replacing local work."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import zipfile


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare(archive: Path, patch: Path, pin: dict, destination: Path) -> dict:
    """Verify both inputs, extract once, check/apply the patch, and record identities.

    Existing destinations are refused, including previous failed attempts. Failed
    preparation remains available for diagnosis; this owner never deletes files.
    It prepares source only and does not certify compilation or editor behavior.
    """
    archive, patch = archive.resolve(strict=True), patch.resolve(strict=True)
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Choose a fresh destination; existing source is preserved")
    if sha256(archive) != pin["archive_sha256"]:
        raise ValueError("Upstream source archive fingerprint differs from the pin")
    if sha256(patch) != pin["patch_sha256"]:
        raise ValueError("Clipline patch fingerprint differs from the pin")
    root_name = pin["archive_root"]
    if not isinstance(root_name, str) or not root_name or any(c in root_name for c in "/\\:") or root_name in {".", ".."}:
        raise ValueError("Invalid archive root pin")
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git is required to check and apply the pinned source patch")
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        seen = set()
        total = 0
        for entry in entries:
            name = PurePosixPath(entry.filename)
            if name.is_absolute() or "\\" in entry.filename or ":" in entry.filename or ".." in entry.filename.split("/"):
                raise ValueError("Unsafe archive path")
            if not name.parts or name.parts[0] != root_name or ".git" in name.parts:
                raise ValueError("Archive entry is outside the pinned source root")
            key = "/".join(name.parts).casefold()
            if key in seen or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("Archive contains a duplicate path or symbolic link")
            seen.add(key)
            total += entry.file_size
            if total > 512 * 1024 * 1024:
                raise ValueError("Source archive exceeds the preparation size limit")
        destination.mkdir(parents=True, exist_ok=False)
        for entry in entries:
            target = destination.joinpath(*PurePosixPath(entry.filename).parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    source_root = destination / root_name
    environment = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        environment.pop(key, None)
    # Keep an enclosing Clipline checkout out of source-patch path resolution.
    environment["GIT_CEILING_DIRECTORIES"] = str(destination)
    commands = []
    for flags in (["--check"], []):
        argv = [git, "-c", "core.autocrlf=false", "-c", "core.eol=lf", "apply", *flags, str(patch)]
        result = subprocess.run(argv, cwd=source_root, env=environment,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        commands.append({"argv": argv, "exit_code": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            receipt = {"status": "failed", "commands": commands}
            (destination / "preparation-failure.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
            raise RuntimeError(f"Pinned patch did not apply: {result.stderr.strip()}")
    files = {}
    for relative, expected in pin.get("patched_files_sha256", {}).items():
        target = (source_root / relative).resolve()
        if not target.is_relative_to(source_root.resolve()) or not target.is_file():
            raise ValueError("Pinned patched file is absent or outside the source root")
        files[relative] = sha256(target)
        if files[relative] != expected:
            failure = {"status": "failed", "commands": commands, "patched_files_sha256": files,
                       "error": "Patched source fingerprint differs from the pin"}
            (destination / "preparation-failure.json").write_text(json.dumps(failure, indent=2), encoding="utf-8")
            raise ValueError(f"Patched source fingerprint differs from the pin: {relative}")
    receipt = {"status": "prepared", "upstream_commit": pin["upstream_commit"],
               "archive_sha256": pin["archive_sha256"], "patch_sha256": pin["patch_sha256"],
               "source_root": str(source_root.resolve()), "commands": commands,
               "patched_files_sha256": files,
               "scope": "source identity and patch application only; compilation and runtime checks are separate"}
    (destination / "prepared-source.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--pin", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    pin = json.loads(args.pin.read_text(encoding="utf-8"))
    print(json.dumps(prepare(args.archive, args.patch, pin, args.destination), indent=2))
