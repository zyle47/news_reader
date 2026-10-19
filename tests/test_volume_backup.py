"""Malicious backups and live/existing Docker resources fail before restore mutation."""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from tools import volume_backup

_IMAGE_ID = "sha256:" + "a" * 64


def _archive(path: Path, member: tarfile.TarInfo | None = None) -> None:
    info = member or tarfile.TarInfo("library/article.txt")
    if member is None:
        info.size = 5
        info.mode = 0o600
        info.uid, info.gid = 10001, 10001
    with tarfile.open(path, "w") as archive:
        archive.addfile(info, io.BytesIO(b"hello") if info.isfile() and info.size else None)
    path.chmod(0o600)


def _backup_directory(path: Path) -> None:
    path.mkdir(mode=0o700)
    for name in ("data.tar", "identity.tar"):
        _archive(path / name)
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "format": 1,
                "complete": True,
                "helper_image_id": _IMAGE_ID,
                "sha256": {
                    name: volume_backup.digest(path / name) for name in ("data.tar", "identity.tar")
                },
            }
        )
    )


class Docker:
    def __init__(self, *, existing: str = "", running: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.existing = existing
        self.running = running
        self.labels: dict[str, str] = {}

    def __call__(self, command: Sequence[str]) -> str:
        args = list(command)
        self.calls.append(args)
        if args[1:3] == ["ps", "-aq"]:
            return "container" if self.running else ""
        if args[1:3] == ["inspect", "container"]:
            return json.dumps([{"State": {"Running": True}}])
        if args[1:3] == ["volume", "ls"]:
            return self.existing
        if args[1:3] == ["image", "inspect"]:
            return json.dumps([{"Id": _IMAGE_ID}])
        if args[1:3] == ["volume", "create"]:
            self.labels = dict(value.split("=", 1) for value in args if "=" in value)
        if args[1:3] == ["volume", "inspect"]:
            return json.dumps([{"Labels": self.labels}])
        return ""

    @property
    def mutations(self) -> list[list[str]]:
        return [
            args for args in self.calls if args[1] == "run" or args[1:3] == ["volume", "create"]
        ]


@pytest.mark.parametrize("name", ["../../outside", "/etc/shadow", "folder/../../escape", "a\\b"])
def test_path_traversal_is_rejected(tmp_path: Path, name: str) -> None:
    info = tarfile.TarInfo(name)
    archive = tmp_path / "malicious.tar"
    _archive(archive, info)
    with pytest.raises(ValueError, match="Unsafe"):
        volume_backup.validate_archive(archive)


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE]
)
def test_links_and_special_files_are_rejected(tmp_path: Path, kind: bytes) -> None:
    info = tarfile.TarInfo("file")
    info.type, info.linkname = kind, "../../outside"
    archive = tmp_path / "malicious.tar"
    _archive(archive, info)
    with pytest.raises(ValueError, match="Unsafe"):
        volume_backup.validate_archive(archive)


def test_duplicate_normalized_paths_are_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "duplicate.tar"
    with tarfile.open(archive, "w") as stream:
        stream.addfile(tarfile.TarInfo("./file"))
        stream.addfile(tarfile.TarInfo("file"))
    archive.chmod(0o600)
    with pytest.raises(ValueError, match="Duplicate"):
        volume_backup.validate_archive(archive)


def test_world_readable_archives_are_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "public.tar"
    _archive(archive)
    archive.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        volume_backup.validate_archive(archive)


def test_malicious_second_archive_is_rejected_before_any_docker_call(tmp_path: Path) -> None:
    source = tmp_path / "backup"
    _backup_directory(source)
    _archive(source / "identity.tar", tarfile.TarInfo("../escape"))
    docker = Docker()
    with pytest.raises(ValueError):
        volume_backup.restore(source, "reader-restore-test", docker)
    assert not docker.calls


def test_corrupted_archive_checksum_prevents_any_restore_mutation(tmp_path: Path) -> None:
    source = tmp_path / "backup"
    _backup_directory(source)
    with (source / "data.tar").open("ab") as stream:
        stream.write(b"changed")
    docker = Docker()
    with pytest.raises(ValueError, match="checksum"):
        volume_backup.restore(source, "reader-restore-test", docker)
    assert not docker.mutations


def test_restore_refuses_existing_destination_without_modifying_it(tmp_path: Path) -> None:
    source = tmp_path / "backup"
    _backup_directory(source)
    docker = Docker(existing="reader-restore-test_data")
    with pytest.raises(ValueError, match="already exist"):
        volume_backup.restore(source, "reader-restore-test", docker)
    assert not docker.mutations


def test_restore_refuses_running_project_before_mutation(tmp_path: Path) -> None:
    source = tmp_path / "backup"
    _backup_directory(source)
    docker = Docker(running=True)
    with pytest.raises(ValueError, match="Stop every"):
        volume_backup.restore(source, "reader-restore-test", docker)
    assert not docker.mutations


def test_restore_stays_unpublished_and_preserves_numeric_owner_contract(tmp_path: Path) -> None:
    source = tmp_path / "backup"
    _backup_directory(source)
    docker = Docker()
    volume_backup.restore(source, "reader-restore-test", docker)
    helpers = [args for args in docker.calls if args[1] == "run"]
    assert len(helpers) == 2
    assert all(args[args.index("--network") + 1] == "none" for args in helpers)
    assert all("--read-only" in args and "numeric_owner=True" in args[-1] for args in helpers)
    assert not any("start" in args or "up" in args for args in docker.calls)
    assert all("onion-info" not in str(args) and "models" not in str(args) for args in helpers)


def test_backup_refuses_running_source_and_keeps_output_absent(tmp_path: Path) -> None:
    output = tmp_path / "backup"
    docker = Docker(running=True)
    with pytest.raises(ValueError, match="Stop every"):
        volume_backup.backup(output, "reader-onion", docker)
    assert not output.exists()
    assert not docker.mutations


@pytest.mark.parametrize("project", ["reader-onion", "../escape", "reader-restore-x;id", ""])
def test_restore_project_name_cannot_target_live_or_inject_options(project: str) -> None:
    with pytest.raises(ValueError):
        volume_backup.project_name(project, restoring=True)
