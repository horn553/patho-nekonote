import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "_site"


def test_pages_build_retains_both_converters_and_omits_server_only_ui():
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_pages.py")],
        cwd=ROOT,
        check=True,
    )

    page = (SITE / "index.html").read_text(encoding="utf-8")
    script = (SITE / "static" / "app.js").read_text(encoding="utf-8")

    assert "画像 → マルチフレームDCM" in page
    assert "PDF → JPG" in page
    assert "convertImagesToDicom" in script
    assert "convertPdfToJpg" in script
    assert (SITE / "static" / "dicom.mjs").is_file()
    assert (SITE / "static" / "github-avatar.png").is_file()
    assert (SITE / "static" / "oss-licenses.html").is_file()
    assert not (SITE / "static" / "login.html").exists()
    assert "uploader-settings" not in page
    assert "/api/settings/uploader" not in script
    assert "dicom-manual" not in page
    assert "DICOMエクスポートマニュアル" not in page
    assert "https://github.com/horn553/patho-nekonote" in page
