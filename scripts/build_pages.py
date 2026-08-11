from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "app" / "static"
OUTPUT_DIR = ROOT / "_site"


def main() -> None:
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)

    published_assets = OUTPUT_DIR / "static"
    shutil.copytree(STATIC_DIR, published_assets)
    shutil.copy2(STATIC_DIR / "index.html", OUTPUT_DIR / "index.html")

    # These server entry points are not part of the browser-only Pages build.
    for server_only_file in ("index.html", "login.html", "login.js"):
        (published_assets / server_only_file).unlink(missing_ok=True)

    (OUTPUT_DIR / ".nojekyll").touch()


if __name__ == "__main__":
    main()
