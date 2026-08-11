# ねこのて

Public repository: <https://github.com/horn553/patho-nekonote>

A small self-hosted collection of file conversion tools. It converts a naturally
ordered ZIP of PNG or JPEG CT slices into one multi-frame DICOM file, and renders
each page of a PDF as a JPG. **Conversion is completely offline in the browser:** source
files and generated results are never uploaded to or stored by the server.

## Run with Docker Compose

The default Compose configuration stores its small runtime database on the HDD,
not beside this repository:

```text
/mnt/hdd/img2dicom  ->  /data inside the container
```

Create the HDD directory once, then start the service:

```bash
mkdir -p /mnt/hdd/img2dicom
docker compose up -d --build
```

Open <http://127.0.0.1:8000>, choose a `.zip`, enter the Study Date and Patient ID,
then select **Convert locally**. For PDF conversion, select **PDF → JPG** on the
same page and choose a `.pdf`. The browser performs all work and creates a local
download without sending either input to the server.

To use another HDD directory or port, create a `.env` file:

```dotenv
IMG2DICOM_DATA_DIR=/mnt/hdd/img2dicom
IMG2DICOM_PORT=8000
IMG2DICOM_UID=1000
IMG2DICOM_GID=1000
```

Set the UID/GID to the owner of the HDD directory (`id -u` and `id -g`). They
default to `1000:1000`, matching the usual first Linux user and this host.

The app binds to localhost by default. For trusted-LAN access, set
`IMG2DICOM_BIND_ADDRESS=0.0.0.0`. The application does not provide authentication;
use an authenticated HTTPS reverse proxy before allowing remote access.

## Input and output

Both tools show in-page local conversion progress. The result is downloaded from
browser memory and remains available only in the current tab. Reloading clears it;
there is deliberately no worklist, server-side retention, or later redownload.

### Images → multi-frame DCM

- Input is one ZIP containing `.png`, `.jpg`, or `.jpeg` files. Other files are
  ignored.
- Slice order uses natural, case-insensitive filenames (`slice2` before
  `slice10`) and descending DICOM patient-Z positions so the stack traverses
  top-to-bottom.
- All images must have identical dimensions.
- Non-interlaced 8-bit and 16-bit grayscale/RGB PNG values are decoded locally;
  16-bit precision is preserved. JPEG and palette PNG images use the browser's
  8-bit decoder. Color values are converted to grayscale.
- The only result is one Explicit VR Little Endian Legacy Converted Enhanced CT
  multi-frame `.dcm` file. Every source image is stored as one frame in natural
  filename order; no per-slice DICOM files or output ZIP are generated.

The upload panel lets you set Patient ID and Study Date for each conversion;
Study Date defaults to January 1 of the current year. Study ID and other descriptive
patient, study, series, and equipment fields are empty or omitted. New Study/Series/SOP
UIDs are generated, while the identity, geometry, and pixel fields required by
DICOM viewers are retained. Geometry defaults to 1 mm square pixels and 1 mm
slice spacing because PNG/JPEG files do not carry reliable CT geometry. Local
conversion is limited to 10,000 images and available browser memory.

### PDF → JPG

- Input is one `.pdf` file. Password-protected PDFs are rejected by PDF.js.
- Pages are rendered in order at 200 DPI in RGB and encoded as quality-90 JPG.
- Output is one ZIP containing `page_0001.jpg`, `page_0002.jpg`, and so on.
- Conversion and ZIP creation happen entirely in browser memory. No PDF page or
  result is transmitted to the server.

> **Important:** outputs are derived data and are not suitable for diagnostic use
> without validation. Confirm slice order, orientation, spacing, calibration, and
> patient/study metadata in a DICOM viewer or validation workflow.

## Development and tests

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```

Or build the production image and run tests without installing locally:

```bash
docker build -t img2dicom .
docker run --rm -v "$PWD:/src:ro" -w /src img2dicom \
  sh -c 'pip install --no-cache-dir -r requirements-dev.txt >/dev/null && python -m pytest'
```

## Architecture

FastAPI serves the shell and locally bundled static assets. The
browser uses JSZip 3.10.1, PDF.js 6.1.200, and the app's Explicit VR Little Endian
Legacy Converted Enhanced CT multi-frame writer. No CDN or remote conversion
service is used. The SQLite database retains compatibility with jobs created by
older deployed versions.

JSZip is bundled under its MIT license and PDF.js under Apache-2.0. Their license
texts are included beside the vendored assets under `app/static/vendor/` and are
linked from the app's **OSSライセンス** page.

## GitHub Pages

The GitHub Pages build publishes the browser-only DICOM and PDF conversion tools.
The legacy server job API remains available only when running the FastAPI
application. Pushes to `main` deploy through
`.github/workflows/deploy-pages.yml` after the Pages source is enabled for the
repository.
