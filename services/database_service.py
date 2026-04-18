from typing import Any

from supabase import Client


class DatabaseService:
    def __init__(self, supabase_client: Client) -> None:
        self.supabase = supabase_client

    def upload_to_storage(self, bucket: str, object_path: str, file_bytes: bytes, content_type: str) -> Any:
        response = self.supabase.storage.from_(bucket).upload(
            object_path,
            file_bytes,
            {"upsert": "true", "content-type": content_type or "application/octet-stream"},
        )
        print(f"[storage.upload] bucket={bucket} path={object_path} response={response}")
        return response

    def move_storage_object(self, bucket: str, old_path: str, new_path: str) -> Any:
        response = self.supabase.storage.from_(bucket).move(old_path, new_path)
        print(f"[storage.move] bucket={bucket} from={old_path} to={new_path} response={response}")
        return response

    def insert_document(
        self,
        file_name: str,
        folder_location: str,
        content_text: str,
        file_size: int,
        mime_type: str,
        category: str = "uncategorized",
        confidence: float = 0,
        status: str = "auto-classified",
    ) -> Any:
        payload = {
            "file_name": file_name,
            "folder_location": folder_location,
            "content_text": content_text,
            "file_size": file_size,
            "mime_type": mime_type,
            "category": category or "uncategorized",
            "confidence": float(confidence or 0),
            "status": status,
        }
        response = self.supabase.table("documents").upsert(payload).execute()
        print(f"[table.insert] file={file_name} path={folder_location} response={self.debug_payload(response)}")
        return response

    def search_documents(self, query: str) -> Any:
        response = (
            self.supabase.table("documents")
            .select("id,file_name,folder_location,file_size,mime_type,category,confidence,status")
            .text_search("search_vector", query)
            .execute()
        )
        print(f"[search.response] payload={self.debug_payload(response)}")
        return response

    # ── Admin: Documents ──────────────────────────────────────────

    def get_all_documents(self) -> Any:
        response = self.supabase.table("documents").select("*").order("id", desc=True).execute()
        return response

    def get_document(self, doc_id: int) -> Any:
        response = self.supabase.table("documents").select("*").eq("id", doc_id).single().execute()
        return response

    def update_document(self, doc_id: int, payload: dict[str, Any]) -> Any:
        response = self.supabase.table("documents").update(payload).eq("id", doc_id).execute()
        return response

    def delete_document(self, doc_id: int) -> Any:
        response = self.supabase.table("documents").delete().eq("id", doc_id).execute()
        return response

    # ── Admin: Categories ─────────────────────────────────────────

    def get_all_categories(self) -> Any:
        response = self.supabase.table("document_categories").select("*").order("id", desc=True).execute()
        return response

    def create_category(self, payload: dict[str, Any]) -> Any:
        response = self.supabase.table("document_categories").insert(payload).execute()
        return response

    def update_category(self, cat_id: int, payload: dict[str, Any]) -> Any:
        response = self.supabase.table("document_categories").update(payload).eq("id", cat_id).execute()
        return response

    def delete_category(self, cat_id: int) -> Any:
        response = self.supabase.table("document_categories").delete().eq("id", cat_id).execute()
        return response

    # ── Admin: Storage helpers ────────────────────────────────────

    def get_download_url(self, path: str, expires_in: int = 120) -> str:
        result = self.supabase.storage.from_("documents").create_signed_url(path, expires_in)
        if isinstance(result, dict):
            return result.get("signedURL") or result.get("signedUrl", "")
        return str(result)

    def delete_storage_object(self, path: str) -> Any:
        response = self.supabase.storage.from_("documents").remove([path])
        return response

    # ── Admin: Stats ──────────────────────────────────────────────

    def get_admin_stats(self) -> dict[str, Any]:
        docs = self.supabase.table("documents").select("id,file_size,category,status").execute()
        cats = self.supabase.table("document_categories").select("id").execute()

        doc_list = docs.data or []
        total_docs = len(doc_list)
        total_cats = len(cats.data or [])
        classified = sum(1 for d in doc_list if (d.get("status") or "").lower() == "classified")
        total_size = sum(int(d.get("file_size") or 0) for d in doc_list)

        # Category breakdown
        breakdown: dict[str, int] = {}
        for d in doc_list:
            cat = d.get("category") or "uncategorized"
            breakdown[cat] = breakdown.get(cat, 0) + 1

        return {
            "total_documents": total_docs,
            "total_categories": total_cats,
            "classified_count": classified,
            "uncategorized_count": total_docs - classified,
            "total_size_bytes": total_size,
            "category_breakdown": breakdown,
        }

    @staticmethod
    def debug_payload(response: Any) -> Any:
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
