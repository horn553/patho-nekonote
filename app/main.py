from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import struct
import sys
import uuid
import zipfile
from array import array
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
import pymupdf
from pydantic import BaseModel
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.tag import Tag
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    LegacyConvertedEnhancedCTImageStorage,
    generate_uid,
)


os.umask(0o077)
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
JOB_DICOM = "dicom"
JOB_PDF_TO_JPG = "pdf_to_jpg"
PDF_DPI = 200
JPEG_QUALITY = 90
TERMINAL_STATUSES = {"completed", "failed"}
ACTIVE_STATUSES = {"uploaded", "queued", "processing"}
SESSION_COOKIE = "img2dicom_session"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if value <= minimum:
        raise RuntimeError(f"{name} must be greater than {minimum}")
    return value


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= minimum:
        raise RuntimeError(f"{name} must be greater than {minimum}")
    return value


class Settings:
    def __init__(self) -> None:
        self.data_dir = Path(os.getenv("IMG2DICOM_DATA_DIR", "/data")).resolve()
        self.jobs_dir = self.data_dir / "jobs"
        self.tmp_dir = self.data_dir / "tmp"
        self.db_path = self.data_dir / "jobs.sqlite3"
        self.max_upload_bytes = int(
            env_float("IMG2DICOM_MAX_UPLOAD_GB", 20) * 1024**3
        )
        self.max_uncompressed_bytes = int(
            env_float("IMG2DICOM_MAX_UNCOMPRESSED_GB", 100) * 1024**3
        )
        self.max_files = env_int("IMG2DICOM_MAX_FILES", 10_000)
        self.pixel_spacing = env_float("IMG2DICOM_PIXEL_SPACING_MM", 1.0)
        self.slice_thickness = env_float(
            "IMG2DICOM_SLICE_THICKNESS_MM", 1.0
        )
        self.password = os.getenv("IMG2DICOM_PASSWORD", "")
        self.retention_hours = env_int("IMG2DICOM_RETENTION_HOURS", 48)

    @property
    def session_token(self) -> str:
        return hashlib.sha256(
            f"img2dicom-session:{self.password}".encode("utf-8")
        ).hexdigest()


settings = Settings()
job_queue: asyncio.Queue[str] = asyncio.Queue()
worker_task: asyncio.Task[None] | None = None
janitor_task: asyncio.Task[None] | None = None


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(settings.db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_storage() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    probe = settings.data_dir / ".write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(f"Data directory is not writable: {settings.data_dir}") from exc

    with db_connect() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                image_count INTEGER,
                progress_current INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0,
                result_bytes INTEGER,
                enhanced_result_bytes INTEGER,
                study_date TEXT,
                patient_id TEXT,
                study_id TEXT,
                job_type TEXT NOT NULL DEFAULT 'dicom',
                error TEXT
            )
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "enhanced_result_bytes" not in columns:
            connection.execute(
                "ALTER TABLE jobs ADD COLUMN enhanced_result_bytes INTEGER"
            )
        if "study_date" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN study_date TEXT")
        if "patient_id" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN patient_id TEXT")
        if "study_id" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN study_id TEXT")
        if "job_type" not in columns:
            connection.execute(
                "ALTER TABLE jobs ADD COLUMN job_type TEXT NOT NULL DEFAULT 'dicom'"
            )
        # Preserve values collected by the short-lived Study ID form under the
        # corrected Patient ID field for jobs that have not converted yet.
        connection.execute(
            "UPDATE jobs SET patient_id = study_id "
            "WHERE (patient_id IS NULL OR patient_id = '') "
            "AND study_id IS NOT NULL AND study_id != ''"
        )
        connection.execute(
            "UPDATE jobs SET status = 'queued', started_at = NULL "
            "WHERE status = 'processing'"
        )
        connection.commit()


def fetch_job(job_id: str) -> dict[str, Any] | None:
    with db_connect() as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return dict(row) if row else None


def update_job(job_id: str, **values: Any) -> None:
    if not values:
        return
    assignments = ", ".join(f"{column} = ?" for column in values)
    with db_connect() as connection:
        connection.execute(
            f"UPDATE jobs SET {assignments} WHERE id = ?",  # noqa: S608
            (*values.values(), job_id),
        )
        connection.commit()


def delete_job_data(job_id: str) -> None:
    shutil.rmtree(settings.jobs_dir / job_id, ignore_errors=True)
    with db_connect() as connection:
        connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        connection.commit()


def cleanup_expired_jobs() -> int:
    cutoff = (datetime.now(UTC) - timedelta(hours=settings.retention_hours)).isoformat(
        timespec="seconds"
    )
    with db_connect() as connection:
        rows = connection.execute(
            "SELECT id FROM jobs WHERE status IN ('completed', 'failed') "
            "AND completed_at IS NOT NULL AND completed_at <= ?",
            (cutoff,),
        ).fetchall()
    for row in rows:
        delete_job_data(row["id"])
    return len(rows)


def public_job(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: row[key]
        for key in (
            "id",
            "job_type",
            "filename",
            "status",
            "created_at",
            "started_at",
            "completed_at",
            "image_count",
            "progress_current",
            "progress_total",
            "result_bytes",
            "enhanced_result_bytes",
            "error",
        )
    }
    if row["completed_at"]:
        completed_at = datetime.fromisoformat(row["completed_at"])
        result["expires_at"] = (
            completed_at + timedelta(hours=settings.retention_hours)
        ).isoformat(timespec="seconds")
    else:
        result["expires_at"] = None
    if row["status"] == "completed":
        result["download_url"] = f"/api/jobs/{row['id']}/download"
        if row["enhanced_result_bytes"] is not None:
            result["enhanced_download_url"] = (
                f"/api/jobs/{row['id']}/download/enhanced"
            )
    return result


def natural_key(name: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", name)]


def default_study_date() -> str:
    """Return January 1 of the current UTC year as a DICOM DA value."""
    return f"{datetime.now(UTC).year:04d}0101"


def normalize_study_date(value: str | None) -> str:
    if value is None or not value.strip():
        return default_study_date()
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError("検査日はYYYY-MM-DD形式の正しい日付を指定してください") from exc
    return f"{parsed.year:04d}{parsed.month:02d}{parsed.day:02d}"


def normalize_patient_id(value: str | None) -> str:
    patient_id = (value or "").strip()
    if len(patient_id) > 64:
        raise ValueError("患者IDは64文字以内で入力してください")
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in patient_id):
        raise ValueError("患者IDには印刷可能なASCII文字のみ使用できます")
    if "\\" in patient_id:
        raise ValueError("患者IDにバックスラッシュは使用できません")
    return patient_id


def safe_image_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members: list[zipfile.ZipInfo] = []
    total_size = 0
    for member in archive.infolist():
        normalized = member.filename.replace("\\", "/")
        path = PurePosixPath(normalized)
        mode = member.external_attr >> 16
        if (
            path.is_absolute()
            or ".." in path.parts
            or (path.parts and path.parts[0].endswith(":"))
        ):
            raise ValueError(f"ZIP内に安全でないパスがあります：{member.filename}")
        if stat.S_ISLNK(mode):
            raise ValueError(f"シンボリックリンクは使用できません：{member.filename}")
        if member.is_dir() or path.suffix.casefold() not in IMAGE_SUFFIXES:
            continue
        members.append(member)
        total_size += member.file_size
        if len(members) > settings.max_files:
            raise ValueError(f"ZIP内の画像数が上限の{settings.max_files}件を超えています")
        if total_size > settings.max_uncompressed_bytes:
            raise ValueError("展開後の画像データが設定された上限を超えています")
    if not members:
        raise ValueError("ZIPにPNGまたはJPEG画像が含まれていません")
    members.sort(key=lambda item: natural_key(item.filename))
    return members


def image_to_pixels(image: Image.Image) -> tuple[bytes, int, int, int, int]:
    image.load()
    if image.mode in {"I;16", "I;16L", "I;16B", "I"}:
        values = [max(0, min(65535, int(value))) for value in image.getdata()]
    else:
        gray = image.convert("L")
        values = list(gray.getdata())

    if not values:
        raise ValueError("画像にピクセルデータがありません")
    pixels = array("H", values)
    if sys.byteorder != "little":
        pixels.byteswap()
    return pixels.tobytes(), image.width, image.height, min(values), max(values)


def dicom_dataset(
    image: Image.Image,
    instance_number: int,
    study_uid: str,
    series_uid: str,
    frame_uid: str,
    study_date: str,
    patient_id: str,
) -> tuple[FileDataset, int, int]:
    pixel_data, columns, rows, minimum, maximum = image_to_pixels(image)
    sop_instance_uid = generate_uid()
    file_meta = FileMetaDataset()
    file_meta.FileMetaInformationVersion = b"\x00\x01"
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()

    dataset = FileDataset(None, {}, file_meta=file_meta, preamble=b"\x00" * 128)
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = sop_instance_uid
    dataset.StudyInstanceUID = study_uid
    dataset.SeriesInstanceUID = series_uid
    dataset.FrameOfReferenceUID = frame_uid
    dataset.Modality = "CT"
    dataset.ImageType = ["DERIVED", "SECONDARY", "AXIAL"]
    dataset.PatientName = ""
    dataset.PatientID = patient_id
    dataset.PatientBirthDate = ""
    dataset.PatientSex = ""
    dataset.StudyDate = study_date
    dataset.StudyTime = ""
    dataset.AccessionNumber = ""
    dataset.ReferringPhysicianName = ""
    dataset.StudyID = ""
    dataset.SeriesNumber = ""
    dataset.AcquisitionNumber = ""
    dataset.InstanceNumber = instance_number

    dataset.Rows = rows
    dataset.Columns = columns
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 16
    dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.PixelSpacing = [settings.pixel_spacing, settings.pixel_spacing]
    dataset.SliceThickness = settings.slice_thickness
    dataset.SpacingBetweenSlices = settings.slice_thickness
    # Patient Z increases from feet to head. Descending positions make the
    # instance sequence traverse the volume from head to feet (top-to-bottom).
    z_position = -(instance_number - 1) * settings.slice_thickness
    dataset.ImagePositionPatient = [0.0, 0.0, z_position]
    dataset.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    dataset.SliceLocation = z_position
    dataset.RescaleIntercept = 0
    dataset.RescaleSlope = 1
    dataset.RescaleType = "US"
    dataset.WindowCenter = (minimum + maximum) / 2
    dataset.WindowWidth = max(1, maximum - minimum)
    dataset.PixelData = pixel_data
    return dataset, minimum, maximum


def enhanced_dicom_dataset(
    *,
    columns: int,
    rows: int,
    frame_count: int,
    minimum: int,
    maximum: int,
    study_uid: str,
    series_uid: str,
    frame_uid: str,
    sop_instance_uid: str,
    study_date: str,
    patient_id: str,
) -> FileDataset:
    file_meta = FileMetaDataset()
    file_meta.FileMetaInformationVersion = b"\x00\x01"
    file_meta.MediaStorageSOPClassUID = LegacyConvertedEnhancedCTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()

    dataset = FileDataset(None, {}, file_meta=file_meta, preamble=b"\x00" * 128)
    dataset.SOPClassUID = LegacyConvertedEnhancedCTImageStorage
    dataset.SOPInstanceUID = sop_instance_uid
    dataset.StudyInstanceUID = study_uid
    dataset.SeriesInstanceUID = series_uid
    dataset.FrameOfReferenceUID = frame_uid
    dataset.Modality = "CT"
    dataset.ImageType = ["DERIVED", "SECONDARY", "AXIAL", "NONE"]
    dataset.PatientName = ""
    dataset.PatientID = patient_id
    dataset.PatientBirthDate = ""
    dataset.PatientSex = ""
    dataset.StudyDate = study_date
    dataset.StudyTime = ""
    dataset.AccessionNumber = ""
    dataset.ReferringPhysicianName = ""
    dataset.StudyID = ""
    dataset.SeriesNumber = ""
    dataset.InstanceNumber = 1
    dataset.PresentationLUTShape = "IDENTITY"
    dataset.AcquisitionContextSequence = []

    dataset.Rows = rows
    dataset.Columns = columns
    dataset.NumberOfFrames = frame_count
    dataset.RepresentativeFrameNumber = 1
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 16
    dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.FrameIncrementPointer = [Tag(0x5200, 0x9230)]

    shared = Dataset()
    pixel_measures = Dataset()
    pixel_measures.PixelSpacing = [settings.pixel_spacing, settings.pixel_spacing]
    pixel_measures.SliceThickness = settings.slice_thickness
    pixel_measures.SpacingBetweenSlices = settings.slice_thickness
    shared.PixelMeasuresSequence = [pixel_measures]

    orientation = Dataset()
    orientation.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    shared.PlaneOrientationSequence = [orientation]

    frame_type = Dataset()
    frame_type.FrameType = ["DERIVED", "SECONDARY", "AXIAL", "NONE"]
    frame_type.PixelPresentation = "MONOCHROME"
    frame_type.VolumetricProperties = "VOLUME"
    frame_type.VolumeBasedCalculationTechnique = "NONE"
    shared.CTImageFrameTypeSequence = [frame_type]

    transformation = Dataset()
    transformation.RescaleIntercept = 0
    transformation.RescaleSlope = 1
    transformation.RescaleType = "US"
    shared.PixelValueTransformationSequence = [transformation]

    voi = Dataset()
    voi.WindowCenter = (minimum + maximum) / 2
    voi.WindowWidth = max(1, maximum - minimum)
    shared.FrameVOILUTSequence = [voi]
    shared.UnassignedSharedConvertedAttributesSequence = [Dataset()]
    dataset.SharedFunctionalGroupsSequence = [shared]

    per_frame = []
    for index in range(1, frame_count + 1):
        frame = Dataset()
        content = Dataset()
        content.StackID = "1"
        content.InStackPositionNumber = index
        frame.FrameContentSequence = [content]

        position = Dataset()
        position.ImagePositionPatient = [
            0.0,
            0.0,
            -(index - 1) * settings.slice_thickness,
        ]
        frame.PlanePositionSequence = [position]
        frame.UnassignedPerFrameConvertedAttributesSequence = [Dataset()]
        per_frame.append(frame)
    dataset.PerFrameFunctionalGroupsSequence = per_frame
    return dataset


def write_native_pixel_data(
    dataset: FileDataset, destination: Path, raw_pixels: Path
) -> None:
    raw_size = raw_pixels.stat().st_size
    padded_size = raw_size + (raw_size % 2)
    if padded_size > 0xFFFFFFFE:
        raise ValueError("マルチフレームDICOMのピクセルデータが4GBの上限を超えています")
    dataset.save_as(destination, enforce_file_format=True)
    with destination.open("ab") as output, raw_pixels.open("rb") as source:
        output.write(struct.pack("<HH2s2sI", 0x7FE0, 0x0010, b"OW", b"\x00\x00", padded_size))
        shutil.copyfileobj(source, output, length=1024 * 1024)
        if raw_size % 2:
            output.write(b"\x00")


def convert_dicom_job(job_id: str) -> None:
    job_dir = settings.jobs_dir / job_id
    input_path = job_dir / "input.zip"
    result_path = job_dir / "dicom.zip"
    enhanced_path = job_dir / "enhanced_ct.dcm"
    raw_pixels_path = job_dir / "enhanced.raw"
    update_job(job_id, status="processing", started_at=utc_now(), error=None)

    try:
        job = fetch_job(job_id)
        if job is None:
            raise ValueError("処理対象が存在しません")
        study_uid = generate_uid()
        series_uid = generate_uid()
        enhanced_series_uid = generate_uid()
        enhanced_sop_instance_uid = generate_uid()
        frame_uid = generate_uid()
        study_date = job.get("study_date") or default_study_date()
        patient_id = job.get("patient_id") or ""

        with zipfile.ZipFile(input_path, "r") as source:
            # Keep source frames in natural filename order. Their DICOM patient
            # Z positions descend, so instances traverse top-to-bottom.
            members = safe_image_members(source)
            update_job(job_id, progress_total=len(members), image_count=len(members))
            expected_size: tuple[int, int] | None = None
            global_minimum = 65535
            global_maximum = 0
            with (
                zipfile.ZipFile(
                    result_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
                ) as output,
                raw_pixels_path.open("wb") as raw_pixels,
            ):
                for index, member in enumerate(members, start=1):
                    try:
                        with source.open(member, "r") as image_file:
                            with Image.open(image_file) as image:
                                size = image.size
                                if expected_size is None:
                                    expected_size = size
                                elif size != expected_size:
                                    raise ValueError(
                                        f"画像サイズが一致しません：{member.filename} は "
                                        f"{size[0]}x{size[1]}、必要なサイズは "
                                        f"{expected_size[0]}x{expected_size[1]} です"
                                    )
                                dataset, minimum, maximum = dicom_dataset(
                                    image,
                                    index,
                                    study_uid,
                                    series_uid,
                                    frame_uid,
                                    study_date,
                                    patient_id,
                                )
                    except (UnidentifiedImageError, OSError) as exc:
                        raise ValueError(f"画像をデコードできません：{member.filename}") from exc

                    buffer = io.BytesIO()
                    dataset.save_as(buffer, enforce_file_format=True)
                    output.writestr(f"CT_{index:06d}.dcm", buffer.getvalue())
                    raw_pixels.write(dataset.PixelData)
                    global_minimum = min(global_minimum, minimum)
                    global_maximum = max(global_maximum, maximum)
                    update_job(job_id, progress_current=index)

                manifest = {
                    "source_filename": fetch_job(job_id)["filename"],
                    "image_count": len(members),
                    "study_instance_uid": study_uid,
                    "series_instance_uid": series_uid,
                    "multi_frame_series_instance_uid": enhanced_series_uid,
                    "multi_frame_sop_instance_uid": enhanced_sop_instance_uid,
                    "pixel_spacing_mm": settings.pixel_spacing,
                    "slice_thickness_mm": settings.slice_thickness,
                    "slice_order": (
                        "natural filename order (top-to-bottom, descending patient Z)"
                    ),
                    "notice": "Derived data; verify geometry and metadata before use.",
                }
                output.writestr("manifest.json", json.dumps(manifest, indent=2))

        enhanced = enhanced_dicom_dataset(
            columns=expected_size[0],
            rows=expected_size[1],
            frame_count=len(members),
            minimum=global_minimum,
            maximum=global_maximum,
            study_uid=study_uid,
            series_uid=enhanced_series_uid,
            frame_uid=frame_uid,
            sop_instance_uid=enhanced_sop_instance_uid,
            study_date=study_date,
            patient_id=patient_id,
        )
        write_native_pixel_data(enhanced, enhanced_path, raw_pixels_path)
        raw_pixels_path.unlink(missing_ok=True)

        input_path.unlink(missing_ok=True)
        update_job(
            job_id,
            status="completed",
            completed_at=utc_now(),
            result_bytes=result_path.stat().st_size,
            enhanced_result_bytes=enhanced_path.stat().st_size,
        )
    except Exception as exc:
        result_path.unlink(missing_ok=True)
        enhanced_path.unlink(missing_ok=True)
        raw_pixels_path.unlink(missing_ok=True)
        update_job(
            job_id,
            status="failed",
            completed_at=utc_now(),
            error=str(exc)[:1000],
        )


def validate_pdf(path: Path) -> int:
    try:
        with pymupdf.open(path) as document:
            if document.needs_pass:
                raise ValueError("パスワード保護されたPDFには対応していません")
            page_count = document.page_count
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDFを開けませんでした") from exc
    if page_count < 1:
        raise ValueError("PDFにページが含まれていません")
    if page_count > settings.max_files:
        raise ValueError(f"PDFのページ数が上限の{settings.max_files}ページを超えています")
    return page_count


def convert_pdf_job(job_id: str) -> None:
    job_dir = settings.jobs_dir / job_id
    input_path = job_dir / "input.pdf"
    result_path = job_dir / "jpg.zip"
    update_job(job_id, status="processing", started_at=utc_now(), error=None)

    try:
        with pymupdf.open(input_path) as document:
            if document.needs_pass:
                raise ValueError("パスワード保護されたPDFには対応していません")
            page_count = document.page_count
            if page_count < 1:
                raise ValueError("PDFにページが含まれていません")
            if page_count > settings.max_files:
                raise ValueError(f"PDFのページ数が上限の{settings.max_files}ページを超えています")

            estimated_rgb_bytes = 0
            scale = PDF_DPI / 72
            for page_number in range(page_count):
                page = document.load_page(page_number)
                width = max(1, math.ceil(page.rect.width * scale))
                height = max(1, math.ceil(page.rect.height * scale))
                estimated_rgb_bytes += width * height * 3
                if estimated_rgb_bytes > settings.max_uncompressed_bytes:
                    raise ValueError("変換後のPDFページが設定されたサイズ上限を超えています")

            update_job(
                job_id,
                progress_total=page_count,
                image_count=page_count,
            )
            with zipfile.ZipFile(
                result_path, "w", compression=zipfile.ZIP_STORED
            ) as output:
                for index in range(1, page_count + 1):
                    page = document.load_page(index - 1)
                    pixmap = page.get_pixmap(
                        dpi=PDF_DPI,
                        colorspace=pymupdf.csRGB,
                        alpha=False,
                    )
                    with Image.frombytes(
                        "RGB",
                        (pixmap.width, pixmap.height),
                        pixmap.samples,
                    ) as image:
                        buffer = io.BytesIO()
                        image.save(
                            buffer,
                            format="JPEG",
                            quality=JPEG_QUALITY,
                            optimize=True,
                        )
                    output.writestr(f"page_{index:04d}.jpg", buffer.getvalue())
                    update_job(job_id, progress_current=index)

        input_path.unlink(missing_ok=True)
        update_job(
            job_id,
            status="completed",
            completed_at=utc_now(),
            result_bytes=result_path.stat().st_size,
            enhanced_result_bytes=None,
        )
    except Exception as exc:
        result_path.unlink(missing_ok=True)
        update_job(
            job_id,
            status="failed",
            completed_at=utc_now(),
            error=str(exc)[:1000],
        )


def convert_job(job_id: str) -> None:
    job = fetch_job(job_id)
    if job is None:
        return
    if job.get("job_type") == JOB_PDF_TO_JPG:
        convert_pdf_job(job_id)
    else:
        convert_dicom_job(job_id)


async def job_worker() -> None:
    while True:
        job_id = await job_queue.get()
        try:
            await asyncio.to_thread(convert_job, job_id)
        finally:
            job_queue.task_done()


async def job_janitor() -> None:
    while True:
        await asyncio.sleep(60 * 60)
        await asyncio.to_thread(cleanup_expired_jobs)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global janitor_task, worker_task
    initialize_storage()
    cleanup_expired_jobs()
    with db_connect() as connection:
        queued = connection.execute(
            "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at"
        ).fetchall()
    for row in queued:
        await job_queue.put(row["id"])
    worker_task = asyncio.create_task(job_worker())
    janitor_task = asyncio.create_task(job_janitor())
    try:
        yield
    finally:
        worker_task.cancel()
        janitor_task.cancel()
        try:
            await asyncio.gather(worker_task, janitor_task)
        except asyncio.CancelledError:
            pass
        worker_task = None
        janitor_task = None


app = FastAPI(
    title="ねこのて",
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class JobStartRequest(BaseModel):
    study_date: str | None = None
    patient_id: str = ""


@app.middleware("http")
async def password_gate(request: Request, call_next):
    if (
        not settings.password
        or request.url.path in {"/login", "/api/health"}
        or request.url.path.startswith("/static/")
    ):
        return await call_next(request)

    supplied = request.cookies.get(SESSION_COOKIE, "")
    if secrets.compare_digest(supplied, settings.session_token):
        return await call_next(request)

    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": "ログインが必要です"})
    return RedirectResponse("/login", status_code=303)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; "
        "img-src 'self' data: blob:; connect-src 'self'; worker-src 'self'; "
        "frame-src https://docs.google.com; object-src 'none'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'"
    )
    # The HTML and JavaScript use stable URLs. Do not let a browser combine an
    # older cached shell with newer startup code, and never cache job metadata.
    if request.url.path.startswith("/static/vendor/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif (
        request.url.path in {"/", "/login"}
        or request.url.path.startswith("/static/")
        or request.url.path.startswith("/api/")
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> FileResponse:
    if not settings.password:
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/login")
async def login(password: str = Form(...)) -> RedirectResponse:
    if not settings.password:
        return RedirectResponse("/", status_code=303)
    if not secrets.compare_digest(password, settings.password):
        return RedirectResponse("/login?error=1", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        settings.session_token,
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/jobs")
async def list_jobs() -> dict[str, list[dict[str, Any]]]:
    cleanup_expired_jobs()
    with db_connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        ]
    return {
        "working": [public_job(row) for row in rows if row["status"] in ACTIVE_STATUSES],
        "worked": [public_job(row) for row in rows if row["status"] in TERMINAL_STATUSES],
    }


@app.post("/api/jobs", status_code=202)
async def create_job(
    file: UploadFile = File(...),
) -> JSONResponse:
    filename = Path(file.filename or "").name
    if not filename or Path(filename).suffix.casefold() != ".zip":
        raise HTTPException(status_code=400, detail=".zipファイルを選択してください")
    job_id = str(uuid.uuid4())
    job_dir = settings.jobs_dir / job_id
    job_dir.mkdir(mode=0o750)
    input_path = job_dir / "input.zip"
    total = 0
    try:
        with input_path.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=413, detail="ファイルサイズが設定された上限を超えています"
                    )
                destination.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="ZIPファイルが空です")
        try:
            with zipfile.ZipFile(input_path, "r") as archive:
                safe_image_members(archive)
        except (zipfile.BadZipFile, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        await file.close()

    created_at = utc_now()
    with db_connect() as connection:
        connection.execute(
            "INSERT INTO jobs (id, job_type, filename, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_id, JOB_DICOM, filename, "uploaded", created_at),
        )
        connection.commit()
    return JSONResponse(
        status_code=202,
        content=public_job(fetch_job(job_id)),
    )


@app.post("/api/jobs/{job_id}/start", status_code=202)
async def start_job(job_id: str, payload: JobStartRequest) -> JSONResponse:
    try:
        study_date = normalize_study_date(payload.study_date)
        patient_id = normalize_patient_id(payload.patient_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with db_connect() as connection:
        result = connection.execute(
            "UPDATE jobs SET status = 'queued', study_date = ?, patient_id = ?, "
            "study_id = '' WHERE id = ? AND status = 'uploaded'",
            (study_date, patient_id, job_id),
        )
        connection.commit()
    if result.rowcount != 1:
        job = fetch_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="処理対象が見つかりません")
        raise HTTPException(status_code=409, detail="この処理はすでに開始されています")

    await job_queue.put(job_id)
    return JSONResponse(status_code=202, content=public_job(fetch_job(job_id)))


@app.post("/api/pdf-jobs", status_code=202)
async def create_pdf_job(file: UploadFile = File(...)) -> JSONResponse:
    filename = Path(file.filename or "").name
    if not filename or Path(filename).suffix.casefold() != ".pdf":
        raise HTTPException(status_code=400, detail=".pdfファイルを選択してください")

    job_id = str(uuid.uuid4())
    job_dir = settings.jobs_dir / job_id
    job_dir.mkdir(mode=0o750)
    input_path = job_dir / "input.pdf"
    total = 0
    try:
        with input_path.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=413, detail="ファイルサイズが設定された上限を超えています"
                    )
                destination.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="PDFファイルが空です")
        try:
            page_count = validate_pdf(input_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        await file.close()

    created_at = utc_now()
    with db_connect() as connection:
        connection.execute(
            "INSERT INTO jobs "
            "(id, job_type, filename, status, created_at, image_count, progress_total) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                JOB_PDF_TO_JPG,
                filename,
                "queued",
                created_at,
                page_count,
                page_count,
            ),
        )
        connection.commit()
    await job_queue.put(job_id)
    return JSONResponse(status_code=202, content=public_job(fetch_job(job_id)))


@app.get("/api/jobs/{job_id}/download")
async def download_job(job_id: str) -> FileResponse:
    cleanup_expired_jobs()
    job = fetch_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="処理対象が見つかりません")
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail="処理が完了していません")
    if job["job_type"] == JOB_PDF_TO_JPG:
        result_path = settings.jobs_dir / job_id / "jpg.zip"
        download_name = f"{Path(job['filename']).stem}_jpg.zip"
    else:
        result_path = settings.jobs_dir / job_id / "dicom.zip"
        download_name = f"{Path(job['filename']).stem}_dicom.zip"
    if not result_path.is_file():
        raise HTTPException(status_code=410, detail="結果ファイルが見つかりません")
    return FileResponse(
        result_path,
        media_type="application/zip",
        filename=download_name,
    )


@app.get("/api/jobs/{job_id}/download/enhanced")
async def download_enhanced_job(job_id: str) -> FileResponse:
    cleanup_expired_jobs()
    job = fetch_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="処理対象が見つかりません")
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail="処理が完了していません")
    if job["job_type"] != JOB_DICOM:
        raise HTTPException(status_code=404, detail="マルチフレームの結果を利用できません")
    result_path = settings.jobs_dir / job_id / "enhanced_ct.dcm"
    if not result_path.is_file():
        raise HTTPException(status_code=410, detail="マルチフレームの結果ファイルが見つかりません")
    download_name = f"{Path(job['filename']).stem}_multiframe.dcm"
    return FileResponse(
        result_path,
        media_type="application/dicom",
        filename=download_name,
    )


@app.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str) -> Response:
    job = fetch_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="処理対象が見つかりません")
    if job["status"] not in TERMINAL_STATUSES | {"uploaded"}:
        raise HTTPException(status_code=409, detail="処理中の項目は削除できません")
    delete_job_data(job_id)
    return Response(status_code=204)
