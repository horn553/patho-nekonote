import io
import sqlite3
import time
import zipfile
from array import array
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pydicom
import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydicom.uid import LegacyConvertedEnhancedCTImageStorage


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("IMG2DICOM_DATA_DIR", str(tmp_path))
    import app.main as main

    test_settings = main.Settings()
    monkeypatch.setattr(main, "settings", test_settings)
    main.job_queue = main.asyncio.Queue()
    with TestClient(main.app) as test_client:
        yield test_client, test_settings


def make_series(names=("slice10.png", "slice2.png", "slice1.png")) -> bytes:
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        for index, name in enumerate(names):
            image_bytes = io.BytesIO()
            Image.new("L", (8, 6), color=20 + index).save(image_bytes, format="PNG")
            archive.writestr(name, image_bytes.getvalue())
    return archive_bytes.getvalue()


def make_pdf(page_count=2) -> bytes:
    document = pymupdf.open()
    try:
        for page_number in range(1, page_count + 1):
            page = document.new_page(width=72, height=72)
            page.insert_text((10, 36), f"Page {page_number}")
        return document.tobytes()
    finally:
        document.close()


def wait_for_completion(client: TestClient, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get("/api/jobs").json()
        if payload["worked"]:
            return payload["worked"][0]
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def upload_job(client: TestClient, filename="scan.zip", content=None):
    response = client.post(
        "/api/jobs",
        files={"file": (filename, content or make_series(), "application/zip")},
    )
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "uploaded"
    return job


def start_job(client: TestClient, job_id: str, metadata=None):
    response = client.post(f"/api/jobs/{job_id}/start", json=metadata or {})
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    return response


def test_upload_converts_series_in_top_to_bottom_order(client):
    test_client, settings = client
    uploaded = upload_job(test_client)

    jobs_before_start = test_client.get("/api/jobs").json()
    assert jobs_before_start["worked"] == []
    assert jobs_before_start["working"][0]["status"] == "uploaded"
    assert (settings.jobs_dir / uploaded["id"] / "input.zip").exists()

    start_job(test_client, uploaded["id"])

    job = wait_for_completion(test_client)
    assert job["status"] == "completed"
    assert job["image_count"] == 3
    assert job["expires_at"] is not None
    assert not (settings.jobs_dir / job["id"] / "input.zip").exists()

    download = test_client.get(job["download_url"])
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as result:
        assert result.namelist() == [
            "CT_000001.dcm",
            "CT_000002.dcm",
            "CT_000003.dcm",
            "manifest.json",
        ]
        first = pydicom.dcmread(io.BytesIO(result.read("CT_000001.dcm")))
        third = pydicom.dcmread(io.BytesIO(result.read("CT_000003.dcm")))
        assert first.Modality == "CT"
        assert first.InstanceNumber == 1
        assert first.Rows == 6
        assert first.Columns == 8
        first_pixels = array("H")
        first_pixels.frombytes(first.PixelData)
        third_pixels = array("H")
        third_pixels.frombytes(third.PixelData)
        assert first_pixels[0] == 22  # natural order starts with slice1.png
        assert third_pixels[0] == 20  # and ends with slice10.png
        assert first.ImagePositionPatient[2] == 0
        assert third.ImagePositionPatient[2] == -2
        assert first.SliceLocation == 0
        assert third.SliceLocation == -2
        assert first.SeriesInstanceUID == third.SeriesInstanceUID
        assert first.StudyDate == f"{datetime.now(UTC).year}0101"
        assert first.PatientID == ""
        assert first.StudyID == ""
        assert first.PatientName == ""
        assert first.StudyTime == ""
        assert "SeriesDescription" not in first
        assert "Manufacturer" not in first
        assert "ContentDate" not in first

    enhanced_download = test_client.get(job["enhanced_download_url"])
    assert enhanced_download.status_code == 200
    enhanced = pydicom.dcmread(io.BytesIO(enhanced_download.content))
    assert enhanced.SOPClassUID == LegacyConvertedEnhancedCTImageStorage
    assert enhanced.Modality == "CT"
    assert enhanced.NumberOfFrames == 3
    assert enhanced.Rows == 6
    assert enhanced.Columns == 8
    assert len(enhanced.PerFrameFunctionalGroupsSequence) == 3
    assert len(enhanced.SharedFunctionalGroupsSequence) == 1
    enhanced_pixels = array("H")
    enhanced_pixels.frombytes(enhanced.PixelData)
    pixels_per_frame = enhanced.Rows * enhanced.Columns
    assert enhanced_pixels[0] == 22
    assert enhanced_pixels[pixels_per_frame * 2] == 20
    first_position = enhanced.PerFrameFunctionalGroupsSequence[
        0
    ].PlanePositionSequence[0].ImagePositionPatient[2]
    last_position = enhanced.PerFrameFunctionalGroupsSequence[
        2
    ].PlanePositionSequence[0].ImagePositionPatient[2]
    assert first_position == 0
    assert last_position == -2
    assert enhanced.StudyDate == f"{datetime.now(UTC).year}0101"
    assert enhanced.PatientID == ""
    assert enhanced.StudyID == ""
    assert enhanced.PatientName == ""
    assert "SeriesDescription" not in enhanced
    assert "Manufacturer" not in enhanced


def test_custom_study_date_and_patient_id_are_applied(client):
    test_client, _ = client
    uploaded = upload_job(test_client, filename="custom.zip")
    start_job(
        test_client,
        uploaded["id"],
        {"study_date": "2024-03-09", "patient_id": "PATIENT-1234"},
    )

    job = wait_for_completion(test_client)
    with zipfile.ZipFile(io.BytesIO(test_client.get(job["download_url"]).content)) as result:
        classic = pydicom.dcmread(io.BytesIO(result.read("CT_000001.dcm")))
    enhanced = pydicom.dcmread(
        io.BytesIO(test_client.get(job["enhanced_download_url"]).content)
    )

    assert classic.StudyDate == "20240309"
    assert classic.PatientID == "PATIENT-1234"
    assert classic.StudyID == ""
    assert enhanced.StudyDate == "20240309"
    assert enhanced.PatientID == "PATIENT-1234"
    assert enhanced.StudyID == ""


def test_pdf_pages_convert_to_downloadable_jpg_zip(client):
    test_client, settings = client
    response = test_client.post(
        "/api/pdf-jobs",
        files={"file": ("report.pdf", make_pdf(2), "application/pdf")},
    )
    assert response.status_code == 202
    queued = response.json()
    assert queued["job_type"] == "pdf_to_jpg"
    assert queued["status"] == "queued"
    assert queued["image_count"] == 2

    job = wait_for_completion(test_client)
    assert job["status"] == "completed"
    assert job["job_type"] == "pdf_to_jpg"
    assert job["image_count"] == 2
    assert job["progress_current"] == 2
    assert job["expires_at"] is not None
    assert "enhanced_download_url" not in job
    assert not (settings.jobs_dir / job["id"] / "input.pdf").exists()

    download = test_client.get(job["download_url"])
    assert download.status_code == 200
    assert 'filename="report_jpg.zip"' in download.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(download.content)) as output:
        assert output.namelist() == ["page_0001.jpg", "page_0002.jpg"]
        for filename in output.namelist():
            with Image.open(io.BytesIO(output.read(filename))) as image:
                assert image.format == "JPEG"
                assert image.mode == "RGB"
                assert image.size == (200, 200)

    enhanced = test_client.get(f"/api/jobs/{job['id']}/download/enhanced")
    assert enhanced.status_code == 404

    job_dir = settings.jobs_dir / job["id"]
    assert test_client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert not job_dir.exists()
    assert test_client.get(job["download_url"]).status_code == 404


def test_pdf_upload_rejects_invalid_input(client):
    test_client, _ = client
    wrong_extension = test_client.post(
        "/api/pdf-jobs",
        files={"file": ("report.txt", make_pdf(), "application/pdf")},
    )
    assert wrong_extension.status_code == 400
    assert ".pdf" in wrong_extension.json()["detail"]

    invalid_pdf = test_client.post(
        "/api/pdf-jobs",
        files={"file": ("broken.pdf", b"not a pdf", "application/pdf")},
    )
    assert invalid_pdf.status_code == 400
    assert "PDFを開けません" in invalid_pdf.json()["detail"]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"study_date": "2024-02-30"}, "正しい日付"),
        ({"patient_id": "bad\\value"}, "バックスラッシュ"),
        ({"patient_id": "x" * 65}, "64文字以内"),
    ],
)
def test_rejects_invalid_metadata(client, data, message):
    test_client, _ = client
    uploaded = upload_job(test_client)
    response = test_client.post(f"/api/jobs/{uploaded['id']}/start", json=data)
    assert response.status_code == 400
    assert message in response.json()["detail"]
    jobs = test_client.get("/api/jobs").json()
    assert jobs["working"][0]["status"] == "uploaded"


def test_rejects_zip_without_images(client):
    test_client, _ = client
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("notes.txt", "no images")
    response = test_client.post(
        "/api/jobs", files={"file": ("empty.zip", payload.getvalue(), "application/zip")}
    )
    assert response.status_code == 400
    assert "含まれていません" in response.json()["detail"]


def test_rejects_unsafe_zip_path(client):
    test_client, _ = client
    response = test_client.post(
        "/api/jobs",
        files={"file": ("unsafe.zip", make_series(("../slice1.png",)), "application/zip")},
    )
    assert response.status_code == 400
    assert "安全でないパス" in response.json()["detail"]


def test_dimension_mismatch_is_reported(client):
    test_client, _ = client
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for name, size in (("1.png", (8, 6)), ("2.png", (9, 6))):
            image = io.BytesIO()
            Image.new("L", size).save(image, format="PNG")
            archive.writestr(name, image.getvalue())
    uploaded = upload_job(
        test_client, filename="mismatch.zip", content=payload.getvalue()
    )
    start_job(test_client, uploaded["id"])
    job = wait_for_completion(test_client)
    assert job["status"] == "failed"
    assert "画像サイズが一致しません" in job["error"]


def test_uploader_link_feature_is_removed(client):
    test_client, _ = client
    assert test_client.get("/api/settings/uploader").status_code == 404
    assert test_client.get("/proceed", follow_redirects=False).status_code == 404


def test_authentication_routes_are_removed(client):
    test_client, _ = client
    assert test_client.get("/").status_code == 200
    assert test_client.get("/api/jobs").status_code == 200
    assert test_client.get("/api/health").status_code == 200
    assert test_client.get("/login").status_code == 404
    assert test_client.post("/login").status_code == 404
    assert test_client.post("/logout").status_code == 404
    assert test_client.get("/static/login.html").status_code == 404
    assert test_client.get("/static/login.js").status_code == 404


def test_local_conversion_ui_and_security_headers(client):
    test_client, _ = client
    for path in (
        "/",
        "/static/app.js",
        "/static/dicom.mjs",
        "/static/github-avatar.png",
        "/static/oss-licenses.html",
        "/api/jobs",
    ):
        response = test_client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"

    for path in (
        "/static/vendor/jszip-3.10.1.min.js",
        "/static/vendor/pdfjs-6.1.200/pdf.min.mjs",
        "/static/vendor/pdfjs-6.1.200/pdf.worker.min.mjs",
    ):
        response = test_client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "javascript" in test_client.get(
        "/static/vendor/pdfjs-6.1.200/pdf.min.mjs"
    ).headers["content-type"]
    assert test_client.get(
        "/static/vendor/pdfjs-6.1.200/wasm/openjpeg.wasm"
    ).headers["content-type"] == "application/wasm"

    page_response = test_client.get("/")
    page = page_response.text
    script = test_client.get("/static/app.js").text
    dicom_script = test_client.get("/static/dicom.mjs").text
    avatar = test_client.get("/static/github-avatar.png")
    licenses = test_client.get("/static/oss-licenses.html").text
    csp = page_response.headers["content-security-policy"]
    assert "worker-src 'self'" in csp
    assert "'wasm-unsafe-eval'" in csp
    assert "docs.google.com" not in csp
    assert '<html lang="ja">' in page
    assert "ねこのて" in page
    assert "🐱" not in page
    assert "🩻" not in page
    assert "📄" not in page
    assert 'src="./static/github-avatar.png"' in page
    assert 'class="tool-icon"' in page
    assert "PDF → JPG" in page
    assert "画像 → マルチフレームDCM" in page
    assert "CTを1ファイルで出力" in page
    assert "Enhanced CTを1ファイルで出力" not in page
    assert "PNG・JPEGスライスを並べ、1つのマルチフレームDCMにまとめます" in page
    assert "上から下の順で" not in page
    assert 'data-tool="pdf"' in page
    assert "100%オフライン変換" in page
    assert "ファイルはアップロードされません" in page
    assert "変換を開始" in page
    assert 'id="result-panel"' in page
    assert "Working" not in page
    assert "Finished" not in page
    assert "Loading previous work" not in page
    assert avatar.headers["content-type"] == "image/png"
    assert "convertImagesToDicom" in script
    assert "convertPdfToJpg" in script
    assert 'fetch("/api/jobs"' not in script
    assert "/api/pdf-jobs" not in script
    assert "FormData" not in script
    assert "XMLHttpRequest" not in script
    assert "createMultiframeDicom" in dicom_script
    assert "LEGACY_CONVERTED_ENHANCED_CT_STORAGE" in dicom_script
    assert "crypto.randomUUID()" not in dicom_script
    assert "cryptography?.getRandomValues" in dicom_script
    assert "_multiframe.dcm" in script
    assert "_dicom.zip" not in script
    assert "decodePngPixels" in dicom_script
    assert "検査日" in page
    assert "患者ID" in page
    assert '<input id="patient-id" type="text" maxlength="64"' in page
    assert "変換を開始" in page
    assert "uploader-settings" not in page
    assert "/api/settings/uploader" not in script
    assert 'href="/proceed"' not in page
    assert "dicom-manual" not in page
    assert "docs.google.com" not in page
    assert "DICOMエクスポートマニュアル" not in page
    assert 'href="./static/oss-licenses.html"' in page
    assert "https://github.com/horn553/patho-nekonote" in page
    assert "JSZip 3.10.1" in licenses
    assert "MIT License" in licenses
    assert "PDF.js 6.1.200" in licenses
    assert "Apache License 2.0" in licenses
    for english_copy in (
        "Choose a ZIP file",
        "Choose file",
        "Download result",
        "Next step",
        "Log out",
        "Welcome back",
    ):
        assert english_copy not in page


def test_uploaded_job_can_be_discarded_before_conversion(client):
    test_client, settings = client
    uploaded = upload_job(test_client, filename="discard.zip")
    job_dir = settings.jobs_dir / uploaded["id"]
    assert job_dir.exists()

    response = test_client.delete(f"/api/jobs/{uploaded['id']}")
    assert response.status_code == 204
    assert not job_dir.exists()
    assert test_client.get("/api/jobs").json()["working"] == []


def test_finished_job_can_be_deleted_manually(client):
    test_client, settings = client
    uploaded = upload_job(test_client, filename="delete.zip")
    start_job(test_client, uploaded["id"])
    job = wait_for_completion(test_client)
    job_dir = settings.jobs_dir / job["id"]
    assert job_dir.exists()

    deleted = test_client.delete(f"/api/jobs/{job['id']}")
    assert deleted.status_code == 204
    assert not job_dir.exists()
    assert test_client.get(job["download_url"]).status_code == 404


def test_finished_job_expires_after_retention_window(client):
    test_client, settings = client
    uploaded = upload_job(test_client, filename="expire.zip")
    start_job(test_client, uploaded["id"])
    job = wait_for_completion(test_client)
    job_dir = settings.jobs_dir / job["id"]
    expired_at = (datetime.now(UTC) - timedelta(hours=49)).isoformat(
        timespec="seconds"
    )
    with sqlite3.connect(settings.db_path) as connection:
        connection.execute(
            "UPDATE jobs SET completed_at = ? WHERE id = ?", (expired_at, job["id"])
        )
        connection.commit()

    jobs = test_client.get("/api/jobs").json()
    assert jobs["worked"] == []
    assert not job_dir.exists()
