import csv
import os
import re
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pypdfium2 as pdfium
import pytesseract
from flask import Flask, jsonify, render_template, request, send_file
from rapidfuzz import fuzz, process
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
EXPORT_DIR = BASE_DIR / "exports"
DB_PATH = BASE_DIR / "data" / "RJSC_Entities.sqlite"

UPLOAD_DIR.mkdir(exist_ok=True)
EXPORT_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {"pdf"}

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "1500"))
FUZZY_MATCH_THRESHOLD = int(os.environ.get("FUZZY_MATCH_THRESHOLD", "92"))
PDF_RENDER_SCALE = float(os.environ.get("PDF_RENDER_SCALE", "1.8"))
BACKGROUND_WORKERS = int(os.environ.get("BACKGROUND_WORKERS", "1"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

JOB_LOCK = threading.Lock()
JOBS: Dict[str, dict] = {}
EXECUTOR = ThreadPoolExecutor(max_workers=BACKGROUND_WORKERS)

SUFFIX_WORDS = [
    "LIMITED", "LTD", "LTD.", "INC", "INC.", "INCORPORATED",
    "CORPORATION", "CORP", "CORP.", "COMPANY", "CO", "CO.",
    "CO-OPERATIVE", "COOPERATIVE", "CO OP", "CO-OP",
    "ASSOCIATION", "SOCIETY", "PARTNERSHIP", "LP", "LLC",
    "HOLDINGS", "ENTERPRISES", "GROUP", "SERVICES", "VENTURES",
]

LEGAL_NOISE = {
    "IN THE MATTER OF", "MATTER OF", "THE COMPANIES ACT", "COMPANIES ACT",
    "REGISTRY OF JOINT STOCK COMPANIES", "PROVINCE OF NOVA SCOTIA",
    "SUPREME COURT OF NOVA SCOTIA", "COURT OF NOVA SCOTIA",
    "CERTIFICATE OF", "NOTICE OF", "FORM OF", "PAGE",
}

BAD_CANDIDATE_CONTAINS = [
    "REGISTRY OF JOINT STOCK", "PROVINCE OF NOVA SCOTIA", "SUPREME COURT",
    "COMPANIES ACT", "PERSONAL PROPERTY", "THIS DOCUMENT", "CERTIFICATE OF STATUS",
    "SCHEDULE", "EXHIBIT", "ROYAL BANK", "BANK OF NOVA SCOTIA",
]


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def normalize_name(value: str) -> str:
    if not value:
        return ""
    value = str(value).upper()
    value = value.replace("’", "'").replace("&AMP;", "&").replace("&", " AND ")
    value = re.sub(r"[^A-Z0-9 ]+", " ", value)
    value = re.sub(r"\bLIMITED\b", "LTD", value)
    value = re.sub(r"\bINCORPORATED\b", "INC", value)
    value = re.sub(r"\bCORPORATION\b", "CORP", value)
    value = re.sub(r"\bCOMPANY\b", "CO", value)
    value = re.sub(r"\bCO OPERATIVE\b", "COOPERATIVE", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_candidate(value: str) -> str:
    value = str(value).upper().replace("\n", " ").replace("’", "'")
    value = re.sub(r"[^A-Z0-9&.'’\- ]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    for phrase in LEGAL_NOISE:
        if value.startswith(phrase):
            value = value.replace(phrase, " ", 1).strip()
    value = re.sub(r"^(OF|THE|A|AN|AND|TO|FOR|RE|NO|NAME)\s+", "", value).strip()
    value = re.sub(r"\s+(OF|THE|AND|FOR|TO|RE|NO|NAME)$", "", value).strip()
    return value


def ensure_database_indexes() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('CREATE INDEX IF NOT EXISTS idx_rjsc_entity_name ON rjsc_entities("Entity Name")')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_rjsc_registry_number ON rjsc_entities("Registry Number")')
    conn.commit()
    conn.close()


def load_business_database() -> Tuple[Dict[str, str], Dict[str, str], List[str], Dict[str, List[str]]]:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database file not found: {DB_PATH}")
    ensure_database_indexes()
    business_lookup: Dict[str, str] = {}
    display_names: Dict[str, str] = {}
    prefix_index: Dict[str, List[str]] = {}
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT "Entity Name", "Registry Number" FROM rjsc_entities WHERE "Entity Name" IS NOT NULL AND "Registry Number" IS NOT NULL')
    for entity_name, registry_number in cur:
        original_name = str(entity_name).strip()
        normalized = normalize_name(original_name)
        if not normalized:
            continue
        if normalized not in business_lookup:
            business_lookup[normalized] = str(registry_number).strip()
            display_names[normalized] = original_name
            words = normalized.split()
            if words:
                prefix_index.setdefault(words[0], []).append(normalized)
    conn.close()
    return business_lookup, display_names, list(business_lookup.keys()), prefix_index


BUSINESS_LOOKUP, DISPLAY_NAMES, BUSINESS_KEYS, PREFIX_INDEX = load_business_database()


def build_page_array(total_pages: int) -> List[int]:
    """
    PDF pages are zero-indexed:
      pdf[0] = page 1
      pdf[1] = page 2
      pdf[2] = page 3

    This scans ONLY page 2 and page 3.
    """
    page_array = []
    for page_index in [1, 2]:
        if page_index < total_pages:
            page_array.append(page_index)
    return page_array


def get_pdf_page_text(page) -> Tuple[str, str]:
    """
    Parse first, OCR second.
    This is faster because searchable PDFs do not need OCR.
    """
    try:
        textpage = page.get_textpage()
        text = textpage.get_text_range()
        textpage.close()
        if text and len(text.strip()) > 40:
            return text, "parsed_text"
    except Exception:
        pass
    try:
        image = page.render(scale=PDF_RENDER_SCALE).to_pil()
        config = "--oem 3 --psm 6"
        text = pytesseract.image_to_string(image, config=config)
        return text or "", "ocr"
    except Exception as e:
        return "", f"ocr_failed: {e}"


def line_candidates(text: str) -> List[str]:
    candidates = set()
    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
    joined = "\n".join(raw_lines)
    suffix_regex = (
        r"(LIMITED|LTD\.?|INC\.?|INCORPORATED|CORPORATION|CORP\.?|"
        r"COMPANY|CO\.?|CO-OPERATIVE|COOPERATIVE|CO-OP|PARTNERSHIP|"
        r"HOLDINGS|ENTERPRISES|GROUP|SERVICES|VENTURES)"
    )
    for line in raw_lines:
        upper = clean_candidate(line)
        if re.search(rf"\b{suffix_regex}\b", upper):
            candidates.add(upper)
    patterns = [
        r"THE\s+NAME\s+OF\s+THE\s+COMPANY\s+IS\s+(.{5,120}?)(?:\n|$)",
        r"IN\s+THE\s+MATTER\s+OF\s+(.{5,120}?)(?:\n|$)",
        r"MATTER\s+OF\s+(.{5,120}?)(?:\n|$)",
        r"RE:\s+(.{5,120}?)(?:\n|$)",
        r"CALLED\s+(.{5,120}?)(?:\n|$)",
    ]
    for pat in patterns:
        for m in re.finditer(pat, joined, flags=re.IGNORECASE | re.DOTALL):
            candidates.add(clean_candidate(m.group(1)))
    compact = re.sub(r"\s+", " ", text.upper())
    for m in re.finditer(rf"\b{suffix_regex}\b", compact):
        start = max(0, m.start() - 90)
        end = min(len(compact), m.end() + 25)
        window = compact[start:end]
        words = re.findall(r"[A-Z0-9&.'\-]+", window)
        for size in range(2, min(10, len(words)) + 1):
            phrase = " ".join(words[-size:])
            if re.search(rf"\b{suffix_regex}\b", phrase):
                candidates.add(clean_candidate(phrase))
    return rank_candidates(list(candidates))


def rank_candidates(candidates: List[str]) -> List[str]:
    cleaned = []
    seen = set()
    for c in candidates:
        c = clean_candidate(c)
        if len(c) < 4 or len(c) > 130:
            continue
        if any(bad in c for bad in BAD_CANDIDATE_CONTAINS):
            continue
        if sum(ch.isalpha() for ch in c) < 4:
            continue
        norm = normalize_name(c)
        if not any(normalize_name(w).replace(".", "") in norm for w in SUFFIX_WORDS):
            continue
        if norm in seen:
            continue
        seen.add(norm)
        cleaned.append(c)
    def score(c: str) -> int:
        norm = normalize_name(c)
        s = 0
        if norm in BUSINESS_LOOKUP:
            s += 2000
        if re.search(r"\b(LTD|LIMITED|INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY)\b", norm):
            s += 200
        if 8 <= len(c) <= 80:
            s += 80
        if len(c.split()) >= 2:
            s += 60
        if len(c.split()) > 9:
            s -= 150
        return s
    cleaned.sort(key=score, reverse=True)
    return cleaned[:30]


def build_match_result(normalized_key: str, match_type: str, score: float) -> dict:
    return {
        "business_name": DISPLAY_NAMES.get(normalized_key, normalized_key),
        "registry_number": BUSINESS_LOOKUP.get(normalized_key, "NOT FOUND"),
        "match_type": match_type,
        "match_score": round(float(score), 2),
    }


def lookup_candidate(candidate: str) -> Optional[dict]:
    norm = normalize_name(candidate)
    if norm in BUSINESS_LOOKUP:
        return build_match_result(norm, "exact", 100)
    loose = re.sub(r"\bTHE\b", "", norm)
    loose = re.sub(r"\s+", " ", loose).strip()
    if loose in BUSINESS_LOOKUP:
        return build_match_result(loose, "normalized", 100)
    words = norm.split()
    choices = BUSINESS_KEYS
    if words and words[0] in PREFIX_INDEX:
        choices = PREFIX_INDEX[words[0]]
    if len(norm) >= 6:
        match = process.extractOne(norm, choices, scorer=fuzz.WRatio, score_cutoff=FUZZY_MATCH_THRESHOLD)
        if match:
            matched_key, score, _ = match
            return build_match_result(matched_key, "fuzzy", score)
    return None


def update_job(job_id: str, **kwargs) -> None:
    with JOB_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def update_file_result(job_id: str, file_index: int, result: dict) -> None:
    with JOB_LOCK:
        JOBS[job_id]["results"][file_index] = result


def process_pdf(pdf_path: Path, original_filename: str, max_pages: Optional[int], job_id: str) -> dict:
    result = {
        "pdf_file": original_filename,
        "business_name": "NOT FOUND",
        "registry_number": "NOT FOUND",
        "page_found": "",
        "match_type": "not_found",
        "match_score": "",
        "text_method": "",
        "status": "processing",
        "notes": "",
    }
    best_candidate = None
    best_candidate_page = None
    best_method = ""
    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
    except Exception as e:
        result.update(status="error", notes=f"Could not open PDF: {e}")
        return result
    try:
        total_pages = len(pdf)
        page_array = build_page_array(total_pages)
        if not page_array:
            result.update(status="error", notes="PDF does not have page 2 or page 3 to scan.")
            return result
        for scan_number, page_index in enumerate(page_array, start=1):
            update_job(
                job_id,
                current_file=original_filename,
                current_page=page_index + 1,
                current_total_pages=total_pages,
                message=(
                    f"Fast parsing scan for {original_filename}: "
                    f"pdf[{page_index}] / page {page_index + 1} "
                    f"({scan_number} of {len(page_array)} selected pages)"
                ),
            )
            page = pdf[page_index]
            text, method = get_pdf_page_text(page)
            try:
                page.close()
            except Exception:
                pass
            candidates = line_candidates(text)
            if candidates and best_candidate is None:
                best_candidate = candidates[0]
                best_candidate_page = page_index + 1
                best_method = method
            for candidate in candidates:
                match = lookup_candidate(candidate)
                if match:
                    result.update(
                        business_name=match["business_name"],
                        registry_number=match["registry_number"],
                        page_found=page_index + 1,
                        match_type=match["match_type"],
                        match_score=match["match_score"],
                        text_method=method,
                        status="completed",
                        notes=f"Matched from candidate: {candidate}",
                    )
                    return result
        if best_candidate:
            result.update(
                business_name=best_candidate,
                registry_number="NOT FOUND",
                page_found=best_candidate_page,
                match_type="candidate_only",
                text_method=best_method,
                status="completed",
                notes="Business name candidate found on page 2 or page 3, but no registry number matched in SQLite database.",
            )
        else:
            result.update(status="completed", notes="No business-name candidate found on page 2 or page 3.")
    except Exception as e:
        result.update(status="error", notes=str(e))
    finally:
        try:
            pdf.close()
        except Exception:
            pass
    return result


def save_results_csv(job_id: str, results: List[dict]) -> Path:
    export_path = EXPORT_DIR / f"rjsc_results_{job_id}.csv"
    with export_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["pdf_file", "business_name", "registry_number"])
        writer.writeheader()
        for row in results:
            writer.writerow({
                "pdf_file": row.get("pdf_file", ""),
                "business_name": row.get("business_name", ""),
                "registry_number": row.get("registry_number", ""),
            })
    return export_path


def process_job(job_id: str, files_to_process: List[dict], max_pages: Optional[int]) -> None:
    update_job(job_id, status="processing", message="Background fast parsing/OCR processing started.")
    try:
        for index, file_info in enumerate(files_to_process):
            update_job(
                job_id,
                processed_files=index,
                current_file=file_info["original_filename"],
                message=f"Processing file {index + 1} of {len(files_to_process)}",
            )
            result = process_pdf(Path(file_info["saved_path"]), file_info["original_filename"], max_pages, job_id)
            update_file_result(job_id, index, result)
            update_job(job_id, processed_files=index + 1)
            with JOB_LOCK:
                partial_results = JOBS[job_id]["results"]
            save_results_csv(job_id, partial_results)
        with JOB_LOCK:
            final_results = JOBS[job_id]["results"]
        save_results_csv(job_id, final_results)
        update_job(
            job_id,
            status="completed",
            message="Processing completed.",
            download_csv_url=f"/download/{job_id}",
            current_file="",
            current_page="",
            current_total_pages="",
        )
    except Exception as e:
        update_job(job_id, status="error", message=str(e))


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "database_loaded": True,
        "business_count": len(BUSINESS_KEYS),
        "max_upload_mb": MAX_UPLOAD_MB,
        "background_workers": BACKGROUND_WORKERS,
        "pdf_render_scale": PDF_RENDER_SCALE,
        "scan_mode": "parse_text_first_then_ocr_page_2_and_page_3_only",
        "pages_scanned": "pdf[1], pdf[2]",
    })


@app.route("/process", methods=["POST"])
def process_uploads():
    if "pdfs" not in request.files:
        return jsonify({"error": "No files uploaded. Use field name 'pdfs'."}), 400
    files = request.files.getlist("pdfs")
    if not files:
        return jsonify({"error": "No files selected."}), 400
    job_id = uuid.uuid4().hex[:12]
    job_upload_dir = UPLOAD_DIR / job_id
    job_upload_dir.mkdir(parents=True, exist_ok=True)
    files_to_process = []
    initial_results = []
    for file in files:
        original_filename = file.filename or "unknown.pdf"
        if not allowed_file(original_filename):
            initial_results.append({
                "pdf_file": original_filename,
                "business_name": "NOT FOUND",
                "registry_number": "NOT FOUND",
                "page_found": "",
                "match_type": "invalid_file",
                "match_score": "",
                "text_method": "",
                "status": "error",
                "notes": "Only PDF files are allowed.",
            })
            continue
        safe_name = secure_filename(original_filename)
        saved_path = job_upload_dir / safe_name
        file.save(saved_path)
        files_to_process.append({"original_filename": original_filename, "saved_path": str(saved_path)})
        initial_results.append({
            "pdf_file": original_filename,
            "business_name": "PENDING",
            "registry_number": "PENDING",
            "page_found": "",
            "match_type": "pending",
            "match_score": "",
            "text_method": "",
            "status": "queued",
            "notes": "Queued for fast page 2/page 3 parsing scan.",
        })
    with JOB_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "message": "Files uploaded. Job queued.",
            "total_files": len(files_to_process),
            "processed_files": 0,
            "current_file": "",
            "current_page": "",
            "current_total_pages": "",
            "results": initial_results,
            "download_csv_url": None,
        }
    if files_to_process:
        EXECUTOR.submit(process_job, job_id, files_to_process, None)
    else:
        update_job(job_id, status="error", message="No valid PDF files were uploaded.")
    return jsonify({
        "job_id": job_id,
        "status_url": f"/status/{job_id}",
        "message": "Upload completed. Background fast parsing scan started.",
    })


@app.route("/status/<job_id>", methods=["GET"])
def job_status(job_id: str):
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job not found."}), 404
        return jsonify(job)


@app.route("/download/<job_id>", methods=["GET"])
def download_csv(job_id: str):
    export_path = EXPORT_DIR / f"rjsc_results_{job_id}.csv"
    if not export_path.exists():
        return jsonify({"error": "CSV is not ready yet."}), 404
    return send_file(export_path, as_attachment=True, download_name=f"rjsc_results_{job_id}.csv", mimetype="text/csv")


@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print(f"Loaded {len(BUSINESS_KEYS):,} business names from SQLite using fast page-array scan.")
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
