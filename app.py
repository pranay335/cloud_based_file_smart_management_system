import os
import threading
import uuid
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from supabase import Client, create_client
from werkzeug.utils import secure_filename

from services.classifier_service import classify_document, set_supabase_client
from services.database_service import DatabaseService
from services.ocr_service import OCRService
from services.pdf_service import PDFService
from services.text_extractor_service import TextExtractorService

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY in environment variables.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
set_supabase_client(supabase)
app = Flask(__name__)
database_service = DatabaseService(supabase)
text_extractor = TextExtractorService(OCRService(), PDFService())

job_store: dict[str, dict[str, Any]] = {}
job_lock = threading.Lock()


def _set_job_state(job_id: str, state: dict[str, Any]) -> None:
    with job_lock:
        job_store[job_id] = state


def _run_background_processing(job_id: str, files_payload: list[dict[str, Any]]) -> None:
    _set_job_state(
        job_id,
        {
            "job_id": job_id,
            "status": "processing",
            "message": "Processing files in background.",
            "processed": 0,
            "total": len(files_payload),
            "details": [],
            "warnings": [],
        },
    )

    details: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []

    try:
        for index, item in enumerate(files_payload, start=1):
            file_name = item["filename"]
            safe_name = secure_filename(file_name)
            storage_path = f"uploads/{uuid.uuid4().hex}_{safe_name}"
            file_bytes = item["bytes"]
            content_type = item.get("mimetype") or "application/octet-stream"

            database_service.upload_to_storage("documents", storage_path, file_bytes, content_type)

            try:
                extraction_result = text_extractor.extract_document(file_name, file_bytes, content_type)
                extracted_text = extraction_result["content_text"]
                file_size = int(extraction_result["file_size"])
                mime_type = str(extraction_result["mime_type"])
                print(
                    f"[text.extract] file={file_name} chars={len(extracted_text)} extension={os.path.splitext(file_name.lower())[1]}"
                )
            except Exception as text_exc:  # noqa: BLE001
                warning = {"file": file_name, "error": str(text_exc)}
                warnings.append(warning)
                print(f"[text.extract.error] file={file_name} error={text_exc}")
                extracted_text = ""
                file_size = len(file_bytes)
                mime_type = content_type

            if not extracted_text:
                print(f"[classification.input] file={file_name} has empty extracted text; using filename-only classification")

            try:
                category, confidence = classify_document(file_name, extracted_text)
            except Exception as classify_exc:  # noqa: BLE001
                warning = {"file": file_name, "error": f"classification failed: {classify_exc}"}
                warnings.append(warning)
                print(f"[classification.error] file={file_name} error={classify_exc}")
                category, confidence = "uncategorized", 0

            final_path = storage_path
            db_status = "classified"

            if (category or "uncategorized").strip().lower() == "uncategorized":
                final_path = storage_path  # keep in uploads/
                db_status = "uncategorized"
            else:
                classified_path = f"classified/{category}/{storage_path.split('/')[-1]}"
                try:
                    supabase.storage.from_("documents").move(storage_path, classified_path)
                    final_path = classified_path
                    db_status = "classified"
                    print(f"[storage.move] Successfully moved to {final_path}")
                except Exception as move_exc:  # noqa: BLE001
                    print(f"[storage.move] Failed: {move_exc}, keeping in uploads/")
                    final_path = storage_path
                    db_status = "uncategorized"
                    category = "uncategorized"
                    confidence = 0

            if category == "uncategorized":
                db_status = "uncategorized"

            database_service.insert_document(
                file_name,
                final_path,
                extracted_text,
                file_size,
                mime_type,
                category,
                confidence,
                db_status,
            )

            details.append(
                {
                    "file": file_name,
                    "category": category,
                    "confidence": confidence,
                    "destination": final_path,
                }
            )

            _set_job_state(
                job_id,
                {
                    "job_id": job_id,
                    "status": "processing",
                    "message": "Processing files in background.",
                    "processed": index,
                    "total": len(files_payload),
                    "details": details,
                    "warnings": warnings,
                },
            )

        _set_job_state(
            job_id,
            {
                "job_id": job_id,
                "status": "completed",
                "message": "Background processing complete.",
                "processed": len(files_payload),
                "total": len(files_payload),
                "details": details,
                "warnings": warnings,
                "source": "local_flask_background",
            },
        )
    except Exception as exc:  # noqa: BLE001
        _set_job_state(
            job_id,
            {
                "job_id": job_id,
                "status": "failed",
                "message": "Background processing failed.",
                "error": str(exc),
                "processed": len(details),
                "total": len(files_payload),
                "details": details,
                "warnings": warnings,
            },
        )
        print(f"[background.error] job_id={job_id} error={exc}")


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

    files_payload: list[dict[str, Any]] = []

    try:
        for file in valid_files:
            file_bytes = file.read()

            if not file_bytes:
                continue

            files_payload.append(
                {
                    "filename": file.filename,
                    "mimetype": file.mimetype,
                    "bytes": file_bytes,
                }
            )

        if not files_payload:
            return jsonify({"error": "Uploaded files were empty."}), 400

        job_id = uuid.uuid4().hex
        _set_job_state(
            job_id,
            {
                "job_id": job_id,
                "status": "queued",
                "message": "Upload received. Processing will start shortly.",
                "processed": 0,
                "total": len(files_payload),
                "details": [],
                "warnings": [],
            },
        )

        worker = threading.Thread(
            target=_run_background_processing,
            args=(job_id, files_payload),
            daemon=True,
        )
        worker.start()

        return (
            jsonify(
                {
                    "job_id": job_id,
                    "status": "queued",
                    "message": "Upload received! Processing in background.",
                    "total": len(files_payload),
                }
            ),
            202,
        )

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


@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job_status(job_id: str):
    with job_lock:
        job = job_store.get(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(job), 200


@app.route("/search", methods=["GET"])
def search_documents():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Missing query parameter: q"}), 400

    try:
        print(f"[search] query={query}")
        response = database_service.search_documents(query)
        return jsonify({"results": response.data or []}), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/download", methods=["GET"])
def download_file():
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "Missing path parameter."}), 400
    try:
        url = database_service.get_download_url(path, expires_in=120)
        return jsonify({"url": url}), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/share", methods=["GET"])
def share_file():
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "Missing path parameter."}), 400
    try:
        url = database_service.get_download_url(path, expires_in=604800)  # 7 days
        return jsonify({"url": url}), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


# ── Admin Dashboard ──────────────────────────────────────────────


@app.route("/admin", methods=["GET"])
def admin_page() -> str:
    return render_template("admin.html")


@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    try:
        stats = database_service.get_admin_stats()
        return jsonify(stats), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/admin/documents", methods=["GET"])
def admin_list_documents():
    try:
        response = database_service.get_all_documents()
        return jsonify({"data": response.data or []}), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/admin/documents/<int:doc_id>", methods=["GET"])
def admin_get_document(doc_id: int):
    try:
        response = database_service.get_document(doc_id)
        return jsonify({"data": response.data}), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/admin/documents/<int:doc_id>", methods=["PUT"])
def admin_update_document(doc_id: int):
    payload = request.get_json(silent=True) or {}
    if not payload:
        return jsonify({"error": "Empty payload."}), 400
    # Only allow safe fields
    allowed = {"file_name", "category", "confidence", "status", "mime_type"}
    filtered = {k: v for k, v in payload.items() if k in allowed}
    if not filtered:
        return jsonify({"error": "No valid fields to update."}), 400
    try:
        response = database_service.update_document(doc_id, filtered)
        return jsonify({"data": response.data}), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/admin/documents/<int:doc_id>", methods=["DELETE"])
def admin_delete_document(doc_id: int):
    try:
        # Get document first so we can also delete the storage file
        doc_resp = database_service.get_document(doc_id)
        doc = doc_resp.data
        folder_location = doc.get("folder_location", "") if doc else ""

        database_service.delete_document(doc_id)

        # Try to delete the storage object too
        if folder_location:
            try:
                database_service.delete_storage_object(folder_location)
            except Exception:  # noqa: BLE001
                pass  # storage delete is best-effort

        return jsonify({"message": "Document deleted."}), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/admin/download", methods=["GET"])
def admin_download():
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "Missing path parameter."}), 400
    try:
        url = database_service.get_download_url(path)
        return jsonify({"url": url}), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/admin/categories", methods=["GET"])
def admin_list_categories():
    try:
        response = database_service.get_all_categories()
        return jsonify({"data": response.data or []}), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/admin/categories", methods=["POST"])
def admin_create_category():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("category_name") or "").strip()
    if not name:
        return jsonify({"error": "category_name is required."}), 400
    insert_data = {
        "category_name": name,
        "keywords": payload.get("keywords", []),
        "extensions": payload.get("extensions", []),
        "score_weight": float(payload.get("score_weight", 1)),
    }
    try:
        response = database_service.create_category(insert_data)
        return jsonify({"data": response.data}), 201
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/admin/categories/<int:cat_id>", methods=["PUT"])
def admin_update_category(cat_id: int):
    payload = request.get_json(silent=True) or {}
    if not payload:
        return jsonify({"error": "Empty payload."}), 400
    allowed = {"category_name", "keywords", "extensions", "score_weight"}
    filtered = {k: v for k, v in payload.items() if k in allowed}
    if not filtered:
        return jsonify({"error": "No valid fields to update."}), 400
    if "score_weight" in filtered:
        filtered["score_weight"] = float(filtered["score_weight"])
    try:
        response = database_service.update_category(cat_id, filtered)
        return jsonify({"data": response.data}), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/admin/categories/<int:cat_id>", methods=["DELETE"])
def admin_delete_category(cat_id: int):
    try:
        database_service.delete_category(cat_id)
        return jsonify({"message": "Category deleted."}), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(debug=True)
