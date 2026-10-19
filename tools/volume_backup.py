#!/usr/bin/env python3
"""Private stopped-volume backup and restore into a new, unpublished Reader project."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

Runner = Callable[[Sequence[str]], str]
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_IMAGE = "reader-onion-app:local"
_ARCHIVES = {"data": "data.tar", "tor-state": "identity.tar"}
_VALIDATE = """
def validate(tar):
    seen = {}
    for member in tar:
        name = member.name
        path = PurePosixPath(name)
        if (not name or len(name) > 4096 or path.is_absolute() or ".." in path.parts
                or "\\\\" in name or any(ord(c) < 32 for c in name)
                or not (member.isfile() or member.isdir())
                or member.mode & 0o7000 or not 0 <= member.uid <= 2147483647
                or not 0 <= member.gid <= 2147483647):
            raise ValueError("Unsafe archive member")
        key = str(path)
        if key in seen or len(seen) >= 1000000 or (key == "." and not member.isdir()):
            raise ValueError("Duplicate or excessive archive members")
        seen[key] = member.isdir()
    for name in seen:
        for parent in PurePosixPath(name).parents:
            if str(parent) in seen and not seen[str(parent)]:
                raise ValueError("Archive file used as a directory")
"""


def run(command: Sequence[str]) -> str:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError as error:
        # Docker stderr may contain private paths or unexpected runtime data.
        raise ValueError(
            "Docker operation failed; inspect the stopped deployment locally."
        ) from error


def project_name(value: str, *, restoring: bool = False) -> str:
    if not _PROJECT.fullmatch(value) or (restoring and not value.startswith("reader-restore-")):
        raise ValueError(
            "Use a valid project name; restore requires a new reader-restore-* project."
        )
    return value


def private_path(path: Path, *, existing: bool) -> Path:
    if not path.is_absolute() or any(character in str(path) for character in ",\n\r"):
        raise ValueError("Use an absolute directory path without commas or newlines.")
    if path.is_symlink() or path.parent.resolve() != path.parent:
        raise ValueError("Backup paths cannot traverse symlinks.")
    if existing:
        if not path.is_dir() or path.stat().st_mode & 0o077:
            raise ValueError("Input directory must exist and have mode 0700.")
    elif path.exists():
        raise ValueError("Backup output must be a new directory.")
    return path


def stopped(project: str, runner: Runner) -> list[dict[str, object]]:
    ids = runner(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]
    ).split()
    records: list[dict[str, object]] = []
    for container_id in ids:
        record = json.loads(runner(["docker", "inspect", container_id]))[0]
        if record["State"]["Running"] or record["State"].get("Restarting"):
            raise ValueError("Stop every container in the project before operating on volumes.")
        records.append({"name": record["Name"], "image_id": record["Image"]})
    return records


def volume_names(runner: Runner) -> set[str]:
    return set(runner(["docker", "volume", "ls", "--format", "{{.Name}}"]).split())


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def helper(image: str, volume: str, directory: Path, script: str, *, restore: bool) -> list[str]:
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "0:0",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "DAC_OVERRIDE",
        "--cap-add",
        "CHOWN",
        "--security-opt",
        "no-new-privileges:true",
        "--entrypoint",
        "python",
        "--mount",
        f"type=volume,source={volume},target=/volume,volume-nocopy"
        + ("" if restore else ",readonly"),
        "--mount",
        f"type=bind,source={directory},target=/backup" + (",readonly" if restore else ""),
    ]
    if restore:
        command += ["--cap-add", "CHOWN", "--cap-add", "FOWNER"]
    return [*command, image, "-c", script]


def backup(output: Path, project: str, runner: Runner = run) -> None:
    project_name(project)
    private_path(output, existing=False)
    records = stopped(project, runner)
    available = volume_names(runner)
    for suffix in _ARCHIVES:
        volume = f"{project}_{suffix}"
        if volume not in available:
            raise ValueError("A required source volume is missing.")
        if runner(["docker", "ps", "-q", "--filter", f"volume={volume}"]).strip():
            raise ValueError("A source volume is mounted by a running container.")
    image_id = json.loads(runner(["docker", "image", "inspect", _IMAGE]))[0]["Id"]
    output.mkdir(mode=0o700)
    complete = False
    try:
        for suffix, archive in _ARCHIVES.items():
            stopped(project, runner)
            script = "import os,tarfile\nos.umask(0o077)\n" + (
                f"with tarfile.open('/backup/{archive}', 'w', dereference=False) as t:\n"
                " t.add('/volume', arcname='.', filter=lambda m: None if "
                "m.name == './models' or m.name.startswith('./models/') else m)\n"
                f"os.chown('/backup/{archive}', {os.getuid()}, {os.getgid()})\n"
            )
            runner(helper(image_id, f"{project}_{suffix}", output, script, restore=False))
            (output / archive).chmod(0o600)
            validate_archive(output / archive)
        manifest = {
            "format": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "project": project,
            "helper_image_id": image_id,
            "containers": records,
            "sha256": {name: digest(output / name) for name in _ARCHIVES.values()},
            "models": "Excluded; reinstall approved catalogue models by checksum.",
            "complete": True,
        }
        with (output / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2)
            stream.write("\n")
        (output / "manifest.json").chmod(0o600)
        complete = True
    finally:
        if not complete:
            # Deliberately retain partial private archives for diagnosis; never call
            # them a complete backup or remove unrelated paths on an error.
            print("Backup incomplete; private partial output retained.")


def validate_archive(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_mode & 0o077:
        raise ValueError("Archive must be a private regular file.")
    namespace: dict[str, object] = {"PurePosixPath": PurePosixPath}
    exec(_VALIDATE, namespace)  # One validation implementation also runs inside the helper.
    with tarfile.open(path, "r:") as archive:
        validator = namespace["validate"]
        assert callable(validator)
        validator(archive)


def restore(source: Path, project: str, runner: Runner = run) -> None:
    project_name(project, restoring=True)
    private_path(source, existing=True)
    manifest_path = source / "manifest.json"
    if not stat.S_ISREG(manifest_path.lstat().st_mode) or manifest_path.stat().st_size > 65536:
        raise ValueError("Invalid backup manifest.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or manifest.get("complete") is not True:
        raise ValueError("Unsupported or incomplete backup.")
    for name in _ARCHIVES.values():
        validate_archive(source / name)
        if digest(source / name) != manifest["sha256"].get(name):
            raise ValueError("Archive checksum does not match the manifest.")
    image_id = manifest.get("helper_image_id", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Invalid backup helper image identifier.")
    stopped(project, runner)
    destinations = {f"{project}_{suffix}" for suffix in _ARCHIVES}
    if destinations & volume_names(runner):
        raise ValueError("Destination volumes already exist; restore never overwrites them.")
    runner(["docker", "image", "inspect", image_id])
    restore_id = uuid4().hex
    # Every archive was validated before any volume creation. A subsequent I/O or
    # daemon failure leaves an unpublished partial restore; never start it blindly.
    try:
        for suffix, name in _ARCHIVES.items():
            volume = f"{project}_{suffix}"
            runner(
                [
                    "docker",
                    "volume",
                    "create",
                    "--label",
                    f"com.docker.compose.project={project}",
                    "--label",
                    f"com.docker.compose.volume={suffix}",
                    "--label",
                    f"reader.restore_id={restore_id}",
                    volume,
                ]
            )
            created = json.loads(runner(["docker", "volume", "inspect", volume]))[0]
            if created.get("Labels", {}).get("reader.restore_id") != restore_id:
                raise ValueError("Destination appeared concurrently; refusing to overwrite it.")
            stopped(project, runner)
            if runner(["docker", "ps", "-q", "--filter", f"volume={volume}"]).strip():
                raise ValueError("Destination volume is mounted by a running container.")
            script = (
                "import tarfile\nfrom pathlib import PurePosixPath\n"
                + _VALIDATE
                + (
                    f"\nwith tarfile.open('/backup/{name}', 'r:') as t:\n"
                    " validate(t)\n"
                    " t.extractall('/volume', numeric_owner=True, filter='fully_trusted')\n"
                )
            )
            runner(helper(image_id, volume, source, script, restore=True))
    except Exception:
        print("Restore incomplete; new unpublished volumes retained for inspection.")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("backup")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--project", default="reader-onion")
    recover = commands.add_parser("restore")
    recover.add_argument("--input", type=Path, required=True)
    recover.add_argument("--project", required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "backup":
            backup(arguments.output, arguments.project)
        else:
            restore(arguments.input, arguments.project)
    except (ValueError, OSError, tarfile.TarError, KeyError) as error:
        print(f"Operation failed: {error}. No deployment was started.")
        return 1
    print("Completed. Keep the backup private; restored volumes remain unpublished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
