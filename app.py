import os
import uuid
from io import BytesIO
from typing import Any

import fitz
import pytesseract
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from PIL import Image
from supabase import Client, create_client
from werkzeug.utils import secure_filename

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Change this path if Tesseract is installed in a different location.
pytesseract.pytesseract.tesseract_cmd = os.getenv(
    "TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
PDF_EXTENSIONS = {".pdf"}
TEXT_EXTENSIONS = {".txt"}
KEYWORD_RULES = {
    "Invoice": ["invoice", "tax", "gst", "amount due", "bill to"],
    "Receipt": ["receipt", "paid", "cash", "total", "transaction"],
    "ID Document": ["passport", "aadhaar", "license", "identity", "dob"],
    "Contract": ["agreement", "contract", "terms", "party", "signature"],
}

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY in environment variables.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)


def _is_image_file(filename: str) -> bool:
    return os.path.splitext(filename.lower())[1] in IMAGE_EXTENSIONS


def _debug_response_payload(response: Any) -> Any:
    if response is None:
        return None
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        try:
            return response.model_dump()
        except Exception:  # noqa: BLE001
            return str(response)
    if hasattr(response, "data"):
        return {"data": getattr(response, "data")}
    return str(response)


def _extract_text_from_file(filename: str, file_bytes: bytes) -> str:
    extension = os.path.splitext(filename.lower())[1]

    if extension in IMAGE_EXTENSIONS:
        return pytesseract.image_to_string(Image.open(BytesIO(file_bytes))).strip()

    if extension in PDF_EXTENSIONS:
        pages: list[str] = []
        with fitz.open(stream=file_bytes, filetype="pdf") as pdf_doc:
            for page in pdf_doc:
                pages.append(page.get_text("text") or "")
        return "\n".join(pages).strip()

    if extension in TEXT_EXTENSIONS:
        try:
            return file_bytes.decode("utf-8").strip()
        except UnicodeDecodeError:
            return file_bytes.decode("latin-1", errors="ignore").strip()

    return ""


def _classify_text_by_keywords(text: str) -> dict[str, Any]:
    normalized = (text or "").lower()
    best_category = "Uncategorized"
    best_score = 0

    for category, keywords in KEYWORD_RULES.items():
        score = sum(1 for keyword in keywords if keyword in normalized)
        if score > best_score:
            best_score = score
            best_category = category

    confidence = round(min(best_score / 3, 1.0), 2) if best_score else 0
    return {"category": best_category, "confidence": confidence}


@app.route("/", methods=["GET"])
def index() -> str:
    return render_template("index.html")


@app.route("/favicon.ico", methods=["GET"])
def favicon():
    return "", 204


@app.route("/api/classify", methods=["POST"])
def classify_documents():
    files = request.files.getlist("files")
    valid_files = [f for f in files if f and f.filename]

    if not valid_files:
        return jsonify({"error": "No files were uploaded."}), 400

    uploaded_paths: list[dict[str, str]] = []
    extracted_text_by_file: dict[str, str] = {}
    extraction_errors: list[dict[str, str]] = []

    try:
        for file in valid_files:
            safe_name = secure_filename(file.filename)
            object_path = f"uploads/{uuid.uuid4().hex}_{safe_name}"
            file_bytes = file.read()

            if not file_bytes:
                continue

            storage_response = supabase.storage.from_("documents").upload(
                object_path,
                file_bytes,
                {"upsert": "true", "content-type": file.mimetype or "application/octet-stream"},
            )
            print(f"[storage.upload] file={file.filename} path={object_path} response={storage_response}")

            extracted_text = ""
            try:
                extracted_text = _extract_text_from_file(file.filename, file_bytes)
                extracted_text_by_file[file.filename] = extracted_text
                print(
                    f"[text.extract] file={file.filename} chars={len(extracted_text)} extension={os.path.splitext(file.filename.lower())[1]}"
                )
            except Exception as text_exc:  # noqa: BLE001
                extraction_errors.append({"file": file.filename, "error": str(text_exc)})
                print(f"[text.extract.error] file={file.filename} error={text_exc}")

            insert_payload = {
                "file_name": file.filename,
                "folder_location": object_path,
                "content_text": extracted_text,
            }
            table_insert_response = supabase.table("documents").insert(insert_payload).execute()
            print(
                f"[table.insert] file={file.filename} path={object_path} response={_debug_response_payload(table_insert_response)}"
            )

            uploaded_paths.append({"file": file.filename, "path": object_path, "ocr_text": extracted_text})

        if not uploaded_paths:
            return jsonify({"error": "Uploaded files were empty."}), 400

        details = []
        for entry in uploaded_paths:
            keyword_result = _classify_text_by_keywords(entry.get("ocr_text", ""))
            details.append(
                {
                    "file": entry["file"],
                    "category": keyword_result["category"],
                    "confidence": keyword_result["confidence"],
                    "destination": entry["path"],
                }
            )

        data = {"details": details, "source": "local_flask"}

        # If OCR failed for any image, include warnings without failing the full request.
        if extraction_errors:
            data["ocr_warnings"] = extraction_errors

        return jsonify(data), 200

    except Exception as exc:  # noqa: BLE001
        error_message = str(exc)
        cause_message = str(getattr(exc, "__cause__", "") or "")
        context_message = str(getattr(exc, "__context__", "") or "")
        combined_error = " | ".join(part for part in [error_message, cause_message, context_message] if part)

        if "row-level security policy" in error_message or "statusCode': 403" in error_message:
            return (
                jsonify(
                    {
                        "error": "Supabase rejected upload (RLS policy). Use a service-role key on the server or add a storage INSERT policy for this bucket/path.",
                        "details": error_message,
                    }
                ),
                403,
            )
        if "Invalid Token or Protected Header formatting" in combined_error:
            return (
                jsonify(
                    {
                        "error": "Supabase auth failed. Ensure SUPABASE_KEY is a valid service_role JWT for server-side storage and table operations.",
                        "details": combined_error,
                    }
                ),
                401,
            )
        if "timed out" in combined_error.lower():
            return (
                jsonify(
                    {
                        "error": "Request timed out while processing upload/search. Try fewer or smaller files and check Supabase latency.",
                        "details": combined_error,
                    }
                ),
                504,
            )
        return jsonify({"error": error_message, "details": combined_error}), 500


@app.route("/search", methods=["GET"])
def search_documents():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Missing query parameter: q"}), 400

    try:
        print(f"[search] query={query}")
        response = (
            supabase.table("documents")
            .select("*")
            .text_search("content_text", query)
            .execute()
        )
        print(f"[search.response] payload={_debug_response_payload(response)}")
        return jsonify({"results": response.data or []}), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(debug=True)
