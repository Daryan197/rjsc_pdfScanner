# RJSC PDF Registry Lookup - Background Processing Version

This version is built for large PDFs.

Instead of uploading a PDF and doing OCR in the same browser request, it now works like this:

```text
Upload PDFs
  ↓
Save PDFs on server
  ↓
Return Job ID immediately
  ↓
Background OCR processing starts
  ↓
Frontend checks /status/<job_id>
  ↓
Results appear when ready
  ↓
Download CSV
```

## Why This Version Is Better for Large Files

Large 40 MB, 50 MB, or bigger PDFs can take a long time to OCR. The older app waited for OCR inside the upload request, which can cause browser or hosting timeouts.

This version separates upload from processing.

## Features

- Multiple PDF uploads
- Large PDF support
- Background OCR queue
- Job progress polling
- SQLite registry lookup
- Optimized SQLite cache
- SQLite indexes
- CSV export
- Render Docker deployment
- No PyMuPDF
- No pandas

## Deploy to Render

1. Extract this ZIP.
2. Upload the extracted project folder to GitHub.
3. Go to Render.
4. Create a new Web Service.
5. Connect the GitHub repo.
6. Use Docker environment.
7. Deploy.

Render will use the included `Dockerfile`, which installs Tesseract automatically.

## Recommended Render Settings

Use at least the Starter plan for large OCR files.

Optional environment variables:

```text
BACKGROUND_WORKERS=1
DEFAULT_MAX_PAGES=15
PDF_RENDER_SCALE=2.2
FUZZY_MATCH_THRESHOLD=92
MAX_UPLOAD_MB=1500
```

## Recommended Usage

For testing, use:

```text
Pages to scan per PDF: 3
```

For regular use, use:

```text
Pages to scan per PDF: 15
```

Only use `all` when necessary, because OCR on every page of very large PDFs can take a long time.

## Local Setup

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

You need Tesseract installed locally for OCR.

If Tesseract is not in PATH on Windows, add this near the top of `app.py`:

```python
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
```

## Important Production Note

Render local disk is temporary. That is okay for uploaded PDFs and CSV exports during a session. For heavy public use, the next upgrade should be Render Web Service + Render Worker + Redis Queue + Cloud Storage for PDFs.
