from __future__ import annotations

import os
import urllib.parse
from datetime import date, datetime, time, timezone
from typing import Any

from app_core.config import AppConfig
from app_core.http import HttpClient


class FirebaseClient:
    def __init__(self, config: AppConfig, http: HttpClient) -> None:
        self.config = config
        self.http = http
        self.base_url = (
            f"https://firestore.googleapis.com/v1/projects/{config.project_id}"
            "/databases/(default)/documents"
        )

    # -------------------------------
    # Firestore value conversion
    # -------------------------------
    def _to_firestore_value(self, value: Any) -> dict[str, Any]:
        if value is None:
            return {"nullValue": None}
        if isinstance(value, bool):
            return {"booleanValue": value}
        if isinstance(value, int) and not isinstance(value, bool):
            return {"integerValue": str(value)}
        if isinstance(value, float):
            return {"doubleValue": value}
        if isinstance(value, datetime):
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
            return {"timestampValue": dt.isoformat().replace("+00:00", "Z")}
        if isinstance(value, date):
            dt = datetime.combine(value, time.min, tzinfo=timezone.utc)
            return {"timestampValue": dt.isoformat().replace("+00:00", "Z")}
        if isinstance(value, list):
            return {
                "arrayValue": {
                    "values": [self._to_firestore_value(item) for item in value]
                }
            }
        if isinstance(value, dict):
            return {
                "mapValue": {
                    "fields": {k: self._to_firestore_value(v) for k, v in value.items()}
                }
            }
        return {"stringValue": str(value)}

    def _from_firestore_value(self, raw: dict[str, Any]) -> Any:
        if "stringValue" in raw:
            return raw["stringValue"]
        if "integerValue" in raw:
            try:
                return int(raw["integerValue"])
            except Exception:
                return raw["integerValue"]
        if "doubleValue" in raw:
            return raw["doubleValue"]
        if "booleanValue" in raw:
            return bool(raw["booleanValue"])
        if "timestampValue" in raw:
            try:
                ts_str = raw["timestampValue"]
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                return dt.replace(tzinfo=None)
            except Exception:
                return raw["timestampValue"]
        if "nullValue" in raw:
            return None
        if "arrayValue" in raw:
            values = raw.get("arrayValue", {}).get("values", [])
            return [self._from_firestore_value(v) for v in values]
        if "mapValue" in raw:
            fields = raw.get("mapValue", {}).get("fields", {})
            return {k: self._from_firestore_value(v) for k, v in fields.items()}
        return None

    # -------------------------------
    # Firestore CRUD
    # -------------------------------
    def add_document(self, collection: str, data: dict[str, Any]) -> tuple[bool, str]:
        if not self.config.firebase_api_key:
            return False, "FIREBASE_API_KEY nao configurada."
        url = f"{self.base_url}/{collection}?key={self.config.firebase_api_key}"
        payload = {"fields": {k: self._to_firestore_value(v) for k, v in data.items()}}
        try:
            response = self.http.post(url, json=payload)
        except Exception as exc:
            return False, str(exc)
        if response.status_code in {200, 201}:
            return True, "Sucesso"
        return False, f"Status {response.status_code}: {response.text}"

    def list_documents(self, collection: str) -> list[dict[str, Any]]:
        if not self.config.firebase_api_key:
            return []
        url = f"{self.base_url}/{collection}?key={self.config.firebase_api_key}"
        try:
            response = self.http.get(url)
        except Exception:
            return []
        if response.status_code != 200:
            return []

        body = response.json()
        documents = body.get("documents", [])
        result: list[dict[str, Any]] = []
        for doc in documents:
            item: dict[str, Any] = {"id": doc.get("name", "").split("/")[-1]}
            fields = doc.get("fields", {})
            for key, value in fields.items():
                item[key] = self._from_firestore_value(value)
            result.append(item)
        return result

    def update_fields(self, collection: str, doc_id: str, fields: dict[str, Any]) -> bool:
        if not self.config.firebase_api_key or not doc_id or not fields:
            return False
        pairs = [(f"updateMask.fieldPaths", field_name) for field_name in fields.keys()]
        pairs.append(("key", self.config.firebase_api_key))
        query = urllib.parse.urlencode(pairs)
        url = f"{self.base_url}/{collection}/{doc_id}?{query}"
        payload = {"fields": {k: self._to_firestore_value(v) for k, v in fields.items()}}
        try:
            response = self.http.patch(url, json=payload)
        except Exception:
            return False
        return response.status_code == 200

    # -------------------------------
    # Storage
    # -------------------------------
    def upload_to_storage(self, file_bytes: bytes, file_name: str, mime_type: str) -> str | None:
        bucket = (self.config.storage_bucket or "").strip()
        if not bucket:
            return None

        safe_name = os.path.basename(file_name or "arquivo")
        safe_name = safe_name.replace(" ", "_")
        date_folder = datetime.now().strftime("%Y-%m-%d")
        storage_path = f"solicitacoes/{date_folder}/{safe_name}"
        encoded_name = urllib.parse.quote(storage_path, safe="")
        url = (
            f"https://firebasestorage.googleapis.com/v0/b/{bucket}/o"
            f"?uploadType=media&name={encoded_name}"
        )

        headers = {"Content-Type": mime_type or "application/octet-stream"}
        try:
            response = self.http.post(url, data=file_bytes, headers=headers)
        except Exception:
            return None

        if response.status_code != 200:
            return None

        payload = response.json()
        token = payload.get("downloadTokens", "")
        public_url = f"https://firebasestorage.googleapis.com/v0/b/{bucket}/o/{encoded_name}?alt=media"
        if token:
            public_url += f"&token={token}"
        return public_url

