"""Backup, restore, and export helpers (plan §8).

Backups are consistent SQLite snapshots plus referenced media, with a manifest,
bounded rotation, and a tested restore path. Credentials are never included:
Gmail tokens and AI keys live in the OS keyring, so a restored copy must be
reconnected rather than inheriting secrets.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import Iterable
from pathlib import Path

from django.utils import timezone

from .models import Application

MANIFEST_NAME = "manifest.json"
DB_NAME = "db.sqlite3"
MEDIA_DIR = "media"


class RestoreError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_database(db_path: Path, dest_path: Path) -> None:
    """Consistent backup using SQLite's online backup API (safe under WAL)."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(db_path))
    target = sqlite3.connect(str(dest_path))
    try:
        with target:
            source.backup(target)
    finally:
        source.close()
        target.close()


def backup_all(
    *,
    db_path: Path,
    media_root: Path,
    backup_dir: Path,
    keep: int = 7,
    when: dt.datetime | None = None,
) -> Path:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    stamp = (when or timezone.now()).astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = Path(backup_dir) / f"jobmon-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)

    backup_database(Path(db_path), dest / DB_NAME)

    if Path(media_root).exists():
        shutil.copytree(Path(media_root), dest / MEDIA_DIR, dirs_exist_ok=True)

    manifest = {
        "created_at": (when or timezone.now()).astimezone(dt.UTC).isoformat(),
        "database": DB_NAME,
        "database_sha256": _sha256(dest / DB_NAME),
        "applications": Application.objects.count(),
        "note": "Credentials are not included; reconnect Gmail/AI after restore.",
    }
    (dest / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    prune_backups(Path(backup_dir), keep=keep)
    return dest


def prune_backups(backup_dir: Path, *, keep: int = 7) -> list[Path]:
    if keep < 1:
        return []
    candidates = sorted(
        (p for p in Path(backup_dir).glob("jobmon-*") if p.is_dir()),
        reverse=True,
    )
    removed = []
    for old in candidates[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old)
    return removed


def verify_backup(backup_path: Path) -> dict:
    backup_path = Path(backup_path)
    manifest_path = backup_path / MANIFEST_NAME
    db_path = backup_path / DB_NAME
    if not manifest_path.exists() or not db_path.exists():
        raise RestoreError(f"Not a valid backup: {backup_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RestoreError("Backup manifest is not valid JSON.") from exc
    if not isinstance(manifest, dict):
        raise RestoreError("Backup manifest must be a JSON object.")
    expected = manifest.get("database_sha256")
    if expected and _sha256(db_path) != expected:
        raise RestoreError("Backup database hash does not match the manifest.")
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RestoreError("Backup database could not be opened.") from exc
    if not result or result[0] != "ok":
        raise RestoreError("Backup database failed SQLite's integrity check.")
    return manifest


def restore_backup(backup_path: Path, *, db_path: Path, media_root: Path) -> dict:
    """Restore a backup over the live database and media. Caller must stop the app."""
    manifest = verify_backup(backup_path)
    db_path = Path(db_path)
    media_root = Path(media_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    media_root.parent.mkdir(parents=True, exist_ok=True)
    backup_media = Path(backup_path) / MEDIA_DIR
    db_staging_root = Path(tempfile.mkdtemp(prefix=".jobmon-db-restore-", dir=db_path.parent))
    media_staging_root = Path(
        tempfile.mkdtemp(prefix=".jobmon-media-restore-", dir=media_root.parent)
    )
    staged_db = db_staging_root / DB_NAME
    staged_media = media_staging_root / MEDIA_DIR
    previous_media = media_root.with_name(f".{media_root.name}.previous-{uuid.uuid4().hex}")
    media_moved = False
    media_swapped = False
    try:
        shutil.copy2(Path(backup_path) / DB_NAME, staged_db)
        if backup_media.exists():
            shutil.copytree(backup_media, staged_media)
        else:
            staged_media.mkdir()

        if media_root.exists():
            os.replace(media_root, previous_media)
            media_moved = True
        os.replace(staged_media, media_root)
        media_swapped = True
        try:
            os.replace(staged_db, db_path)
        except OSError:
            shutil.rmtree(media_root, ignore_errors=True)
            media_swapped = False
            if media_moved:
                os.replace(previous_media, media_root)
                media_moved = False
            raise
        for suffix in ("-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    except OSError as exc:
        if media_swapped:
            shutil.rmtree(media_root, ignore_errors=True)
        if media_moved and previous_media.exists() and not media_root.exists():
            os.replace(previous_media, media_root)
        raise RestoreError(f"Could not replace the live backup data: {exc}") from exc
    finally:
        shutil.rmtree(db_staging_root, ignore_errors=True)
        shutil.rmtree(media_staging_root, ignore_errors=True)
    shutil.rmtree(previous_media, ignore_errors=True)
    return manifest


def csv_rows(queryset: Iterable[Application]) -> list[list[str]]:
    header = [
        "company",
        "title",
        "stage",
        "priority",
        "source",
        "application_date",
        "closing_date",
        "location",
        "work_arrangement",
        "employment_type",
        "requisition_id",
        "job_url",
        "salary",
        "updated_at",
    ]
    rows = [header]
    for application in queryset:
        rows.append(
            [
                application.company,
                application.title,
                application.get_stage_display(),
                application.get_priority_display(),
                application.get_source_display(),
                application.application_date.isoformat() if application.application_date else "",
                application.closing_date.isoformat() if application.closing_date else "",
                application.location,
                application.get_work_arrangement_display(),
                application.get_employment_type_display(),
                application.requisition_id,
                application.job_url,
                application.salary_display,
                application.updated_at.isoformat(),
            ]
        )
    return rows
