from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import requests


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "sync.sqlite3"
LOCAL_TZ_OFFSET = 7 * 3600

AUDIO_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".amr",
    ".flac",
    ".m4a",
    ".mp3",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}

DEFAULT_ALLOWED_PIPELINE_NAMES = {
    "Отдел продаж",
    "Юридический отдел",
    "Отдел завершения",
    "Успешно завершенные",
    "Техническая воронка",
    "Прогрев базы",
    "TG / Max - Боты",
    "[А7] TG / Max - Боты",
}


def load_env() -> dict[str, str]:
    values: dict[str, str] = dict(os.environ)
    path = ROOT / ".env"
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*([^#=]+)=(.*)$", raw_line)
        if not match:
            continue
        values[match.group(1).strip()] = match.group(2).strip().strip('"').strip("'")
    return values


ENV = load_env()


def env(name: str, default: str = "") -> str:
    return ENV.get(name, default).strip()


def require_env(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def safe_name(value: str, max_len: int = 140) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.strip(" ._")
    if not value:
        return "unnamed"
    return value[:max_len].rstrip(" ._")


def format_ts(timestamp: int) -> str:
    if not timestamp:
        return "no-date"
    return datetime.fromtimestamp(timestamp + LOCAL_TZ_OFFSET, timezone.utc).strftime("%Y-%m-%d_%H-%M")


def normalize_disk_path(raw: str) -> str:
    value = extract_disk_value(raw)
    if not value:
        raise ValueError("empty Yandex Disk folder value")

    if value.startswith("disk:"):
        value = value[len("disk:") :]
    elif value.startswith("yadisk:"):
        value = value[len("yadisk:") :]
    elif value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        if parsed.netloc not in {"disk.yandex.ru", "yadi.sk"}:
            raise ValueError(f"unsupported folder URL host: {parsed.netloc}")
        if parsed.path.startswith("/client/disk/"):
            value = "/" + unquote(parsed.path[len("/client/disk/") :])
        elif parsed.path == "/client/disk":
            value = "/"
        else:
            raise ValueError(
                "public share links like https://disk.yandex.ru/d/... cannot be used as upload paths; "
                "put disk:/folder/path or the owner browser URL /client/disk/..."
            )

    value = value.replace("\\", "/")
    value = re.sub(r"/+", "/", value).strip()
    if value.startswith("/"):
        value = value[1:]
    if not value:
        raise ValueError("empty Yandex Disk folder path")
    return value.strip("/")


def extract_disk_value(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""

    markdown_url = extract_markdown_url(value)
    if markdown_url:
        value = markdown_url

    value = value.strip().strip("<>")
    value = value.replace("\\)", ")").replace("\\(", "(")
    return value


def extract_markdown_url(value: str) -> str:
    marker = "]("
    start = value.find(marker)
    if not (value.startswith("[") and start > 0):
        return ""

    pos = start + len(marker)
    chars: list[str] = []
    escaped = False
    while pos < len(value):
        char = value[pos]
        if escaped:
            chars.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ")":
            break
        else:
            chars.append(char)
        pos += 1
    return "".join(chars).strip()


def crm_files_folder(client_folder: str) -> str:
    folder = normalize_disk_path(client_folder)
    subfolder = env("YANDEX_CRM_FILES_SUBFOLDER", "Файлы из CRM").strip().strip("/\\")
    if not subfolder:
        return folder
    if folder.lower().replace("\\", "/").endswith("/" + subfolder.lower()):
        return folder
    if folder.lower() == subfolder.lower():
        return folder
    return f"{folder}/{subfolder}"


def configured_disk_root() -> str:
    return normalize_disk_path(env("YANDEX_DISK_SCAN_ROOT", env("YANDEX_DISK_TEST_ROOT", "test-CRM")))


def is_disk_path_under_root(path: str, root: str | None = None) -> bool:
    folder = normalize_disk_path(path)
    allowed_root = normalize_disk_path(root or configured_disk_root())
    folder_key = folder.casefold()
    root_key = allowed_root.casefold()
    return folder_key == root_key or folder_key.startswith(root_key + "/")


def crm_files_folder_in_configured_root(client_folder: str) -> str:
    folder = normalize_disk_path(client_folder)
    root = configured_disk_root()
    if not is_disk_path_under_root(folder, root):
        raise ValueError(f"folder is outside configured Yandex Disk root disk:/{root}")
    return crm_files_folder(folder)


def yandex_client_url(folder_path: str) -> str:
    path = normalize_disk_path(folder_path)
    return "https://disk.yandex.ru/client/disk/" + quote(path, safe="/()_-.,")


def guess_is_audio(file_name: str, mime_type: str | None) -> bool:
    if (mime_type or "").lower().startswith("audio/"):
        return True
    return Path(file_name).suffix.lower() in AUDIO_EXTENSIONS


class Store:
    def __init__(self, path: Path = DB_PATH) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self.connect() as conn:
            existing = conn.execute("select name from sqlite_master where type = 'table' and name = 'uploads'").fetchone()
            if existing:
                columns = {row["name"] for row in conn.execute("pragma table_info(uploads)").fetchall()}
                if "source_kind" not in columns:
                    conn.execute("drop table uploads")

            conn.executescript(
                """
                create table if not exists uploads (
                    source_kind text not null,
                    source_id text not null,
                    source_entity_type text not null,
                    source_entity_id integer not null,
                    note_id integer,
                    file_uuid text not null,
                    version_uuid text not null default '',
                    target_path text not null,
                    file_name text,
                    file_size integer,
                    uploaded_at integer not null,
                    primary key (source_kind, source_id, file_uuid, version_uuid, target_path)
                );

                create table if not exists settings (
                    key text primary key,
                    value text not null
                );

                create table if not exists processed_events (
                    event_id text primary key,
                    event_type text not null,
                    lead_id integer not null,
                    created_at integer not null,
                    processed_at integer not null
                );

                create table if not exists processed_disk_folders (
                    folder_path text primary key,
                    lead_id integer not null,
                    field_value text not null,
                    processed_at integer not null
                );

                create table if not exists disk_folder_no_match (
                    folder_path text primary key,
                    folder_name text not null,
                    folder_modified text not null,
                    scanned_at integer not null
                );
                """
            )

    def has_upload(self, item: "Attachment", target_path: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                select 1 from uploads
                where source_kind = ? and source_id = ? and file_uuid = ? and version_uuid = ? and target_path = ?
                """,
                (item.source_kind, item.source_id, item.file_uuid, item.version_uuid or "", target_path),
            ).fetchone()
        return bool(row)

    def save_upload(self, item: "Attachment", target_path: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert or replace into uploads(
                    source_kind, source_id, source_entity_type, source_entity_id, note_id, file_uuid, version_uuid,
                    target_path, file_name, file_size, uploaded_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.source_kind,
                    item.source_id,
                    item.source_entity_type,
                    item.source_entity_id,
                    item.note_id,
                    item.file_uuid,
                    item.version_uuid or "",
                    target_path,
                    item.file_name,
                    item.size,
                    int(time.time()),
                ),
            )

    def get_setting_int(self, key: str, default: int = 0) -> int:
        with self.connect() as conn:
            row = conn.execute("select value from settings where key = ?", (key,)).fetchone()
        return int(row["value"]) if row else default

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("select value from settings where key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting_int(self, key: str, value: int) -> None:
        self.set_setting(key, str(value))

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "insert into settings(key, value) values(?, ?) on conflict(key) do update set value = excluded.value",
                (key, value),
            )

    def delete_setting(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("delete from settings where key = ?", (key,))

    def has_event(self, event_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("select 1 from processed_events where event_id = ?", (event_id,)).fetchone()
        return bool(row)

    def save_event(self, event: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert or ignore into processed_events(event_id, event_type, lead_id, created_at, processed_at)
                values(?, ?, ?, ?, ?)
                """,
                (
                    str(event.get("id") or ""),
                    str(event.get("type") or ""),
                    int(event.get("entity_id") or 0),
                    int(event.get("created_at") or 0),
                    int(time.time()),
                ),
            )

    def has_disk_folder(self, folder_path: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("select 1 from processed_disk_folders where folder_path = ?", (folder_path,)).fetchone()
        return bool(row)

    def save_disk_folder(self, folder_path: str, lead_id: int, field_value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert or replace into processed_disk_folders(folder_path, lead_id, field_value, processed_at)
                values(?, ?, ?, ?)
                """,
                (folder_path, lead_id, field_value, int(time.time())),
            )

    def has_disk_folder_no_match(self, folder_path: str, folder_modified: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                select 1 from disk_folder_no_match
                where folder_path = ? and folder_modified = ?
                """,
                (folder_path, folder_modified),
            ).fetchone()
        return bool(row)

    def save_disk_folder_no_match(self, folder_path: str, folder_name: str, folder_modified: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert or replace into disk_folder_no_match(folder_path, folder_name, folder_modified, scanned_at)
                values(?, ?, ?, ?)
                """,
                (folder_path, folder_name, folder_modified, int(time.time())),
            )


@dataclass(frozen=True)
class Attachment:
    source_kind: str
    source_id: str
    source_entity_type: str
    source_entity_id: int
    lead_id: int
    note_id: int | None
    created_at: int
    file_uuid: str
    version_uuid: str
    file_name: str
    original_name: str
    size: int | None = None
    mime_type: str | None = None
    download_url: str | None = None

    @property
    def display_name(self) -> str:
        return self.original_name or self.file_name or f"{self.file_uuid}.bin"


class AmoClient:
    def __init__(self) -> None:
        self.base_url = require_env("AMOCRM_BASE_URL").rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"Authorization": f"Bearer {require_env('AMOCRM_ACCESS_TOKEN')}"})
        self.drive_url: str | None = None

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=60)
        if response.status_code == 204:
            return {}
        response.raise_for_status()
        return response.json()

    def patch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.patch(f"{self.base_url}{path}", json=payload, timeout=60)
        if response.status_code == 204:
            return {}
        response.raise_for_status()
        return response.json() if response.content else {}

    def list_embedded(self, path: str, key: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page = 1
        params = dict(params or {})
        while True:
            params.update({"page": page})
            payload = self.get(path, params)
            batch = payload.get("_embedded", {}).get(key, []) or []
            result.extend(batch)
            if not batch or "next" not in payload.get("_links", {}):
                return result
            page += 1

    def account_drive_url(self) -> str:
        if not self.drive_url:
            payload = self.get("/api/v4/account", {"with": "drive_url"})
            self.drive_url = str(payload.get("drive_url") or "").rstrip("/")
            if not self.drive_url:
                raise RuntimeError("amoCRM account did not return drive_url")
        return self.drive_url

    def get_file_meta(self, file_uuid: str) -> dict[str, Any]:
        response = self.session.get(f"{self.account_drive_url()}/v1.0/files/{file_uuid}", timeout=60)
        response.raise_for_status()
        return response.json()

    def list_pipeline_ids_by_names(self, names: set[str]) -> dict[int, str]:
        pipelines = self.list_embedded("/api/v4/leads/pipelines", "pipelines", {"limit": 250})
        return {int(pipeline["id"]): str(pipeline.get("name") or "") for pipeline in pipelines if str(pipeline.get("name") or "") in names}

    def allowed_pipeline_ids(self) -> dict[int, str]:
        configured = env("AMOCRM_ALLOWED_PIPELINE_IDS")
        if configured:
            ids = {int(item.strip()) for item in configured.split(",") if item.strip()}
            pipelines = self.list_embedded("/api/v4/leads/pipelines", "pipelines", {"limit": 250})
            names = {int(pipeline["id"]): str(pipeline.get("name") or "") for pipeline in pipelines}
            return {pipeline_id: names.get(pipeline_id, str(pipeline_id)) for pipeline_id in ids}
        result = self.list_pipeline_ids_by_names(DEFAULT_ALLOWED_PIPELINE_NAMES)
        missing = sorted(DEFAULT_ALLOWED_PIPELINE_NAMES - set(result.values()))
        if missing:
            print(f"Warning: allowed pipeline names not found: {', '.join(missing)}")
        return result

    def list_latest_leads(self, limit: int) -> list[dict[str, Any]]:
        allowed = self.allowed_pipeline_ids()
        leads: list[dict[str, Any]] = []
        for pipeline_id in allowed:
            payload = self.get(
                "/api/v4/leads",
                {
                    "limit": min(max(limit, 1), 250),
                    "filter[pipeline_id]": pipeline_id,
                    "order[created_at]": "desc",
                    "with": "contacts",
                },
            )
            leads.extend(payload.get("_embedded", {}).get("leads", []) or [])
        leads.sort(key=lambda lead: int(lead.get("created_at") or 0), reverse=True)
        return leads[:limit]

    def get_lead(self, lead_id: int) -> dict[str, Any]:
        return self.get(f"/api/v4/leads/{lead_id}", {"with": "contacts"})

    def search_leads(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        payload = self.get("/api/v4/leads", {"query": query, "limit": min(max(limit, 1), 250), "with": "contacts"})
        return payload.get("_embedded", {}).get("leads", []) or []

    def update_lead_folder_field(self, lead_id: int, field_id: int, field_value: str) -> None:
        self.patch(
            f"/api/v4/leads/{lead_id}",
            {
                "custom_fields_values": [
                    {
                        "field_id": field_id,
                        "values": [{"value": field_value}],
                    }
                ]
            },
        )

    def iter_leads_with_folder_field(
        self,
        field_id: int,
        since_updated_at: int | None,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        allowed = self.allowed_pipeline_ids()
        params: dict[str, Any] = {
            "limit": 250,
            "with": "contacts",
            "order[updated_at]": "asc",
            "filter[pipeline_id][]": list(allowed.keys()),
        }
        if since_updated_at:
            params["filter[updated_at][from]"] = since_updated_at

        leads: list[dict[str, Any]] = []
        max_updated_at = since_updated_at or 0
        page = 1
        while True:
            params["page"] = page
            payload = self.get("/api/v4/leads", params)
            batch = payload.get("_embedded", {}).get("leads", []) or []
            for lead in batch:
                max_updated_at = max(max_updated_at, int(lead.get("updated_at") or 0))
                if lead_folder_value(lead, field_id):
                    leads.append(lead)
                    if limit and len(leads) >= limit:
                        return leads, max_updated_at
            if not batch or "next" not in payload.get("_links", {}):
                return leads, max_updated_at
            page += 1

    def list_field_change_events(self, field_id: int, from_ts: int, to_ts: int | None = None) -> list[dict[str, Any]]:
        event_type = f"custom_field_{field_id}_value_changed"
        params: dict[str, Any] = {
            "limit": 250,
            "filter[entity]": "lead",
            "filter[type][]": event_type,
            "filter[created_at][from]": from_ts,
            "order[created_at]": "asc",
        }
        if to_ts:
            params["filter[created_at][to]"] = to_ts
        return self.list_embedded("/api/v4/events", "events", params)

    def resolve_folder_field_id(self) -> int:
        configured = env("AMOCRM_YANDEX_FOLDER_FIELD_ID")
        if configured:
            return int(configured)

        expected_name = env("AMOCRM_YANDEX_FOLDER_FIELD_NAME", "Папка Яндекс.Диска").strip().lower()
        fields = self.list_embedded("/api/v4/leads/custom_fields", "custom_fields", {"limit": 250})
        for field in fields:
            if str(field.get("name") or "").strip().lower() == expected_name:
                return int(field["id"])

        candidates = [
            {"id": f.get("id"), "name": f.get("name"), "type": f.get("type")}
            for f in fields
            if any(word in str(f.get("name") or "").lower() for word in ["диск", "yandex", "яндекс", "папк"])
        ]
        raise RuntimeError(
            "Yandex folder lead field was not found. Set AMOCRM_YANDEX_FOLDER_FIELD_ID "
            f"or create a field named {expected_name!r}. Similar fields: {json.dumps(candidates, ensure_ascii=False)}"
        )

    def lead_contact_ids(self, lead: dict[str, Any]) -> list[int]:
        contacts = lead.get("_embedded", {}).get("contacts", []) or []
        return [int(contact["id"]) for contact in contacts if contact.get("id")]

    def list_attachment_notes(self, entity_type: str, entity_id: int) -> list[dict[str, Any]]:
        return self.list_embedded(
            f"/api/v4/{entity_type}/{entity_id}/notes",
            "notes",
            {"limit": 250, "filter[note_type]": "attachment"},
        )

    def list_entity_files(self, entity_type: str, entity_id: int) -> list[dict[str, Any]]:
        return self.list_embedded(f"/api/v4/{entity_type}/{entity_id}/files", "files", {"limit": 250})

    def attachment_from_file_ref(
        self,
        lead_id: int,
        entity_type: str,
        entity_id: int,
        source_kind: str,
        file_ref: dict[str, Any],
    ) -> Attachment | None:
        file_uuid = str(file_ref.get("file_uuid") or file_ref.get("uuid") or "")
        if not file_uuid:
            return None
        source_id = str(file_ref.get("id") or file_uuid)
        item = Attachment(
            source_kind=source_kind,
            source_id=source_id,
            source_entity_type=entity_type,
            source_entity_id=entity_id,
            lead_id=lead_id,
            note_id=None,
            created_at=int(file_ref.get("created_at") or file_ref.get("updated_at") or 0),
            file_uuid=file_uuid,
            version_uuid=str(file_ref.get("version_uuid") or ""),
            file_name=str(file_ref.get("name") or ""),
            original_name=str(file_ref.get("name") or ""),
        )
        return self.hydrate_attachment(item)

    def hydrate_attachment(self, item: Attachment) -> Attachment:
        meta = self.get_file_meta(item.file_uuid)
        links = meta.get("_links") or {}
        metadata = meta.get("metadata") or {}
        name = str(meta.get("name") or item.file_name or item.original_name or item.file_uuid)
        extension = str(metadata.get("extension") or "").strip(".")
        if extension and not Path(name).suffix:
            name = f"{name}.{extension}"
        mime_type = metadata.get("mime_type") or mimetypes.guess_type(name)[0]
        if not mime_type and str(meta.get("type") or "").lower() in {"audio", "voice"}:
            mime_type = "audio/unknown"
        return Attachment(
            **{
                **item.__dict__,
                "created_at": int(meta.get("created_at") or item.created_at or 0),
                "file_name": name,
                "original_name": item.original_name or name,
                "version_uuid": str(meta.get("version_uuid") or item.version_uuid or ""),
                "size": meta.get("size"),
                "mime_type": mime_type,
                "download_url": ((links.get("download_version") or {}).get("href"))
                or ((links.get("download") or {}).get("href")),
            }
        )

    def collect_attachments(self, lead: dict[str, Any], include_contact_notes: bool = True) -> list[Attachment]:
        lead_id = int(lead["id"])
        sources: list[tuple[str, int]] = [("leads", lead_id)]
        if include_contact_notes:
            sources.extend(("contacts", contact_id) for contact_id in self.lead_contact_ids(lead))

        items: list[Attachment] = []
        seen_files: set[tuple[str, str]] = set()
        for entity_type, entity_id in sources:
            source_kind = "lead_file" if entity_type == "leads" else "contact_file"
            for file_ref in self.list_entity_files(entity_type, entity_id):
                try:
                    item = self.attachment_from_file_ref(lead_id, entity_type, entity_id, source_kind, file_ref)
                except requests.RequestException as exc:
                    print(f"  file meta failed: lead={lead_id} file_ref={file_ref.get('id')} error={exc}")
                    continue
                if not item:
                    continue
                key = (item.file_uuid, item.version_uuid or "")
                if key in seen_files:
                    continue
                seen_files.add(key)
                items.append(item)

            notes = self.list_attachment_notes(entity_type, entity_id)
            for note in notes:
                params = note.get("params") or {}
                file_uuid = str(params.get("file_uuid") or "")
                if not file_uuid:
                    continue
                version_uuid = str(params.get("version_uuid") or "")
                key = (file_uuid, version_uuid)
                if key in seen_files:
                    continue
                seen_files.add(key)

                item = Attachment(
                    source_kind="lead_note" if entity_type == "leads" else "contact_note",
                    source_id=str(note["id"]),
                    source_entity_type=entity_type,
                    source_entity_id=entity_id,
                    lead_id=lead_id,
                    note_id=int(note["id"]),
                    created_at=int(note.get("created_at") or 0),
                    file_uuid=file_uuid,
                    version_uuid=version_uuid,
                    file_name=str(params.get("file_name") or ""),
                    original_name=str(params.get("original_name") or params.get("file_name") or ""),
                )
                try:
                    item = self.hydrate_attachment(item)
                except requests.RequestException as exc:
                    print(f"  file meta failed: lead={lead_id} note={note.get('id')} uuid={file_uuid} error={exc}")
                items.append(item)
        items.sort(key=lambda item: (item.created_at, item.source_kind, item.source_id, item.display_name))
        return items


class YandexDiskClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"OAuth {require_env('YANDEX_DISK_TOKEN')}"})

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        response = self.session.request(method, f"https://cloud-api.yandex.net/v1/disk{path}", timeout=90, **kwargs)
        if response.status_code not in {200, 201, 202, 204, 409}:
            response.raise_for_status()
        return response

    def ensure_folder(self, folder_path: str) -> None:
        parts = [part for part in normalize_disk_path(folder_path).split("/") if part]
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            if self.exists(current):
                continue
            last_response: requests.Response | None = None
            for attempt in range(1, 4):
                response = self.session.put(
                    "https://cloud-api.yandex.net/v1/disk/resources",
                    params={"path": current},
                    timeout=90,
                )
                if response.status_code in {201, 409}:
                    break
                if response.status_code == 423 and attempt < 3:
                    time.sleep(attempt * 2)
                    continue
                last_response = response
                break
            if last_response is not None:
                last_response.raise_for_status()

    def exists(self, path: str) -> bool:
        response = self.session.get(
            "https://cloud-api.yandex.net/v1/disk/resources",
            params={"path": path},
            timeout=60,
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    def list_dir(self, folder_path: str, limit: int = 1000) -> list[dict[str, Any]]:
        path = normalize_disk_path(folder_path)
        page_limit = min(max(limit, 1), 1000)
        offset = 0
        result: list[dict[str, Any]] = []
        while True:
            response = self.request(
                "GET",
                "/resources",
                params={"path": path, "limit": page_limit, "offset": offset},
            )
            embedded = response.json().get("_embedded", {}) or {}
            batch = embedded.get("items", []) or []
            result.extend(batch)
            total = int(embedded.get("total") or 0)
            offset += len(batch)
            if not batch or len(batch) < page_limit or (total and offset >= total):
                return result

    def upload_file(self, local_path: Path, target_path: str, overwrite: bool = False) -> None:
        response = self.request(
            "GET",
            "/resources/upload",
            params={"path": target_path, "overwrite": str(overwrite).lower()},
        )
        if response.status_code == 409:
            response.raise_for_status()
        href = response.json()["href"]
        with local_path.open("rb") as handle:
            upload_response = requests.put(href, data=handle, timeout=600)
        if upload_response.status_code == 409:
            upload_response.raise_for_status()
        upload_response.raise_for_status()


def lead_folder_value(lead: dict[str, Any], field_id: int) -> str:
    for field in lead.get("custom_fields_values") or []:
        if int(field.get("field_id") or 0) != field_id:
            continue
        values = field.get("values") or []
        if not values:
            return ""
        return str(values[0].get("value") or "").strip()
    return ""


def test_target_folder(lead: dict[str, Any]) -> str:
    root = normalize_disk_path(env("YANDEX_DISK_TEST_ROOT", "CRM Test"))
    lead_name = safe_name(str(lead.get("name") or "lead"))
    return crm_files_folder(f"{root}/{int(lead['id'])}_{lead_name}")


def item_target_path(folder: str, item: Attachment) -> str:
    source = "contact" if item.source_entity_type == "contacts" else "lead"
    name = safe_name(item.display_name)
    source_id = safe_name(f"{item.source_kind}_{item.source_id}", max_len=80)
    return f"{folder}/{format_ts(item.created_at)}_{source}_{source_id}_{name}"


def download_attachment(amo: AmoClient, item: Attachment, tmp_dir: Path) -> Path:
    if not item.download_url:
        meta = amo.get_file_meta(item.file_uuid)
        links = meta.get("_links") or {}
        download_url = ((links.get("download_version") or {}).get("href")) or ((links.get("download") or {}).get("href"))
    else:
        download_url = item.download_url
    if not download_url:
        raise RuntimeError(f"No download link for amoCRM file uuid={item.file_uuid}")

    out_path = tmp_dir / safe_name(item.display_name)
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with amo.session.get(download_url, stream=True, timeout=180) as response:
                response.raise_for_status()
                with out_path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 512):
                        if chunk:
                            handle.write(chunk)
            return out_path
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(attempt * 2)
    raise last_error or RuntimeError(f"Failed to download {item.file_uuid}")


def upload_with_retry(disk: YandexDiskClient, local_path: Path, target_path: str) -> None:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            disk.upload_file(local_path, target_path, overwrite=False)
            return
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(attempt * 2)
    raise last_error or RuntimeError(f"Failed to upload {target_path}")


def sync_lead(
    amo: AmoClient,
    disk: YandexDiskClient,
    store: Store,
    lead: dict[str, Any],
    folder: str,
    force: bool = False,
    dry_run: bool = False,
    include_contact_notes: bool = True,
    skip_audio: bool = True,
) -> dict[str, int]:
    lead_id = int(lead["id"])
    stats = {"attachments": 0, "uploaded": 0, "skipped": 0, "errors": 0}
    print(f"Lead {lead_id}: {lead.get('name') or ''}")
    print(f"  target: disk:/{folder}")

    if not dry_run:
        disk.ensure_folder(folder)

    attachments = amo.collect_attachments(lead, include_contact_notes=include_contact_notes)
    stats["attachments"] = len(attachments)
    if not attachments:
        print("  no files")
        return stats

    with tempfile.TemporaryDirectory(prefix="sinai-yd-") as tmp:
        tmp_dir = Path(tmp)
        for item in attachments:
            target_path = item_target_path(folder, item)
            if skip_audio and guess_is_audio(item.display_name, item.mime_type):
                stats["skipped"] += 1
                print(f"  skip audio: source={item.source_kind}/{item.source_id} file={item.display_name}")
                continue
            if not force and store.has_upload(item, target_path):
                stats["skipped"] += 1
                print(f"  skip already uploaded: source={item.source_kind}/{item.source_id} file={item.display_name}")
                continue

            try:
                if dry_run:
                    print(f"  would upload: source={item.source_kind}/{item.source_id} file={item.display_name} -> {target_path}")
                    stats["skipped"] += 1
                    continue
                local_path = download_attachment(amo, item, tmp_dir)
                upload_with_retry(disk, local_path, target_path)
                store.save_upload(item, target_path)
                stats["uploaded"] += 1
                print(f"  uploaded: source={item.source_kind}/{item.source_id} file={item.display_name}")
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                body = exc.response.text[:300] if exc.response is not None else ""
                if status == 409:
                    store.save_upload(item, target_path)
                    stats["skipped"] += 1
                    print(f"  skip exists on disk: source={item.source_kind}/{item.source_id} file={item.display_name}")
                else:
                    stats["errors"] += 1
                    print(f"  error http={status}: source={item.source_kind}/{item.source_id} file={item.display_name} {body}")
            except Exception as exc:
                stats["errors"] += 1
                print(f"  error: source={item.source_kind}/{item.source_id} file={item.display_name} {exc}")

    return stats


def command_test_last(args: argparse.Namespace) -> int:
    amo = AmoClient()
    disk = YandexDiskClient()
    store = Store()
    leads = amo.list_latest_leads(args.limit)
    total = {"attachments": 0, "uploaded": 0, "skipped": 0, "errors": 0}
    for lead in leads:
        stats = sync_lead(
            amo,
            disk,
            store,
            lead,
            test_target_folder(lead),
            force=args.force,
            dry_run=args.dry_run,
            include_contact_notes=not args.no_contact_notes,
            skip_audio=not args.include_audio,
        )
        for key, value in stats.items():
            total[key] += value
    print("Summary:", json.dumps(total, ensure_ascii=False))
    return 1 if total["errors"] else 0


def command_test_leads(args: argparse.Namespace) -> int:
    amo = AmoClient()
    disk = YandexDiskClient()
    store = Store()
    allowed = amo.allowed_pipeline_ids()
    total = {"attachments": 0, "uploaded": 0, "skipped": 0, "errors": 0}
    for lead_id in args.lead_id:
        lead = amo.get_lead(int(lead_id))
        pipeline_id = int(lead.get("pipeline_id") or 0)
        if pipeline_id not in allowed:
            total["skipped"] += 1
            print(
                f"Lead {lead_id}: skip pipeline_id={pipeline_id}; "
                f"allowed pipelines: {', '.join(allowed.values())}"
            )
            continue
        stats = sync_lead(
            amo,
            disk,
            store,
            lead,
            test_target_folder(lead),
            force=args.force,
            dry_run=args.dry_run,
            include_contact_notes=not args.no_contact_notes,
            skip_audio=not args.include_audio,
        )
        for key, value in stats.items():
            total[key] += value
    print("Summary:", json.dumps(total, ensure_ascii=False))
    return 1 if total["errors"] else 0


def command_sync_field_leads(args: argparse.Namespace) -> int:
    amo = AmoClient()
    disk = YandexDiskClient()
    store = Store()
    field_id = amo.resolve_folder_field_id()
    allowed = amo.allowed_pipeline_ids()
    total = {"attachments": 0, "uploaded": 0, "skipped": 0, "errors": 0}

    print(f"Folder field id: {field_id}")
    for lead_id in args.lead_id:
        lead = amo.get_lead(int(lead_id))
        pipeline_id = int(lead.get("pipeline_id") or 0)
        if pipeline_id not in allowed:
            total["skipped"] += 1
            print(
                f"Lead {lead_id}: skip pipeline_id={pipeline_id}; "
                f"allowed pipelines: {', '.join(allowed.values())}"
            )
            continue

        raw_folder = lead_folder_value(lead, field_id)
        if not raw_folder:
            total["skipped"] += 1
            print(f"Lead {lead_id}: skip empty Yandex Disk folder field")
            continue

        try:
            folder = crm_files_folder_in_configured_root(raw_folder)
        except ValueError as exc:
            total["skipped"] += 1
            print(f"Lead {lead_id}: skip folder outside configured root {raw_folder!r}: {exc}")
            continue

        stats = sync_lead(
            amo,
            disk,
            store,
            lead,
            folder,
            force=args.force,
            dry_run=args.dry_run,
            include_contact_notes=not args.no_contact_notes,
            skip_audio=not args.include_audio,
        )
        for key, value in stats.items():
            total[key] += value

    print("Summary:", json.dumps(total, ensure_ascii=False))
    return 1 if total["errors"] else 0


def command_sync_by_field(args: argparse.Namespace) -> int:
    amo = AmoClient()
    disk = YandexDiskClient()
    store = Store()
    field_id = amo.resolve_folder_field_id()
    since = None if args.all else store.get_setting_int("last_lead_updated_at", 0)
    if args.since:
        since = int(datetime.fromisoformat(args.since).timestamp())

    print(f"Folder field id: {field_id}")
    print(f"Lead updated_at from: {since or 'beginning'}")
    leads, max_updated_at = amo.iter_leads_with_folder_field(field_id, since_updated_at=since, limit=args.limit or None)
    print(f"Leads with filled folder field: {len(leads)}")

    total = {"attachments": 0, "uploaded": 0, "skipped": 0, "errors": 0}
    for lead in leads:
        raw_folder = lead_folder_value(lead, field_id)
        try:
            folder = crm_files_folder_in_configured_root(raw_folder)
        except ValueError as exc:
            total["skipped"] += 1
            print(f"Lead {lead.get('id')}: skip folder outside configured root {raw_folder!r}: {exc}")
            continue
        stats = sync_lead(
            amo,
            disk,
            store,
            lead,
            folder,
            force=args.force,
            dry_run=args.dry_run,
            include_contact_notes=not args.no_contact_notes,
            skip_audio=not args.include_audio,
        )
        for key, value in stats.items():
            total[key] += value

    if not args.dry_run and not args.all and max_updated_at:
        store.set_setting_int("last_lead_updated_at", max_updated_at)
    print("Summary:", json.dumps(total, ensure_ascii=False))
    return 1 if total["errors"] else 0


def sync_field_events_once(args: argparse.Namespace, amo: AmoClient, disk: YandexDiskClient, store: Store) -> int:
    field_id = amo.resolve_folder_field_id()
    allowed = amo.allowed_pipeline_ids()
    now = int(time.time())
    setting_key = f"last_event_created_at_field_{field_id}"

    if args.since:
        from_ts = int(datetime.fromisoformat(args.since).timestamp())
    else:
        saved_ts = store.get_setting_int(setting_key, 0)
        fallback = now - int(args.lookback_hours * 3600)
        from_ts = saved_ts or fallback
        if saved_ts:
            from_ts = max(0, saved_ts - 60)

    print(f"Folder field id: {field_id}")
    print(f"Events from: {from_ts}")
    events = amo.list_field_change_events(field_id, from_ts=from_ts, to_ts=now)
    print(f"Field change events: {len(events)}")

    total = {"attachments": 0, "uploaded": 0, "skipped": 0, "errors": 0}
    max_created_at = store.get_setting_int(setting_key, 0)
    for event in events:
        event_id = str(event.get("id") or "")
        created_at = int(event.get("created_at") or 0)
        max_created_at = max(max_created_at, created_at)
        if event_id and store.has_event(event_id):
            total["skipped"] += 1
            print(f"Event {event_id}: skip already processed")
            continue

        lead_id = int(event.get("entity_id") or 0)
        if not lead_id:
            total["skipped"] += 1
            print(f"Event {event_id}: skip empty lead id")
            continue

        try:
            lead = amo.get_lead(lead_id)
            pipeline_id = int(lead.get("pipeline_id") or 0)
            if pipeline_id not in allowed:
                total["skipped"] += 1
                print(f"Event {event_id}: skip lead={lead_id} pipeline_id={pipeline_id}")
                if not args.dry_run:
                    store.save_event(event)
                continue

            raw_folder = lead_folder_value(lead, field_id) or event_folder_value(event, field_id)
            if not raw_folder:
                total["skipped"] += 1
                print(f"Event {event_id}: skip lead={lead_id} empty folder value")
                if not args.dry_run:
                    store.save_event(event)
                continue

            try:
                folder = crm_files_folder_in_configured_root(raw_folder)
            except ValueError as exc:
                total["skipped"] += 1
                print(f"Event {event_id}: skip lead={lead_id} folder outside configured root {raw_folder!r}: {exc}")
                if not args.dry_run:
                    store.save_event(event)
                continue
            stats = sync_lead(
                amo,
                disk,
                store,
                lead,
                folder,
                force=args.force,
                dry_run=args.dry_run,
                include_contact_notes=not args.no_contact_notes,
                skip_audio=not args.include_audio,
            )
            for key, value in stats.items():
                total[key] += value
            if not args.dry_run and stats["errors"] == 0:
                store.save_event(event)
        except Exception as exc:
            total["errors"] += 1
            print(f"Event {event_id}: error lead={lead_id} {exc}")

    if not args.dry_run and max_created_at:
        store.set_setting_int(setting_key, max_created_at)
    print("Summary:", json.dumps(total, ensure_ascii=False))
    return 1 if total["errors"] else 0


def command_sync_field_events(args: argparse.Namespace) -> int:
    return sync_field_events_once(args, AmoClient(), YandexDiskClient(), Store())


def command_monitor_field_events(args: argparse.Namespace) -> int:
    amo = AmoClient()
    disk = YandexDiskClient()
    store = Store()
    while True:
        sync_field_events_once(args, amo, disk, store)
        time.sleep(args.interval_seconds)


def event_folder_value(event: dict[str, Any], field_id: int) -> str:
    for value in event.get("value_after") or []:
        custom = value.get("custom_field_value") or {}
        if int(custom.get("field_id") or 0) != field_id:
            continue
        return str(custom.get("text") or custom.get("value") or "").strip()
    return ""


def scan_disk_folders_once(args: argparse.Namespace, amo: AmoClient, disk: YandexDiskClient, store: Store) -> int:
    field_id = amo.resolve_folder_field_id()
    allowed = amo.allowed_pipeline_ids()
    root = normalize_disk_path(env("YANDEX_DISK_SCAN_ROOT", env("YANDEX_DISK_TEST_ROOT", "test-CRM")))
    print(f"Disk scan root: disk:/{root}")

    folders = [item for item in disk.list_dir(root) if item.get("type") == "dir"]
    print(f"Disk folders found: {len(folders)}")

    total = {"matched": 0, "updated": 0, "attachments": 0, "uploaded": 0, "skipped": 0, "errors": 0}
    for folder in folders:
        folder_name = str(folder.get("name") or "")
        folder_path = str(folder.get("path") or "").replace("disk:/", "", 1)
        if not folder_path or store.has_disk_folder(folder_path):
            continue

        try:
            lead = find_lead_for_disk_folder(amo, folder_name, allowed)
            if not lead:
                print(f"Disk folder {folder_name!r}: no matching lead")
                continue

            lead_id = int(lead["id"])
            total["matched"] += 1
            field_value = yandex_client_url(folder_path)
            current_value = lead_folder_value(lead, field_id)
            if not current_value or normalize_disk_path(current_value) != normalize_disk_path(field_value):
                if args.dry_run:
                    print(f"Disk folder {folder_name!r}: would update lead={lead_id} field={field_value}")
                else:
                    amo.update_lead_folder_field(lead_id, field_id, field_value)
                    print(f"Disk folder {folder_name!r}: updated lead={lead_id} Yandex folder field")
                    total["updated"] += 1

            stats = sync_lead(
                amo,
                disk,
                store,
                lead,
                crm_files_folder(folder_path),
                force=args.force,
                dry_run=args.dry_run,
                include_contact_notes=not args.no_contact_notes,
                skip_audio=not args.include_audio,
            )
            for key in ("attachments", "uploaded", "skipped", "errors"):
                total[key] += stats[key]
            if not args.dry_run and stats["errors"] == 0:
                store.save_disk_folder(folder_path, lead_id, field_value)
        except Exception as exc:
            total["errors"] += 1
            print(f"Disk folder {folder_name!r}: error {exc}")

    print("Disk scan summary:", json.dumps(total, ensure_ascii=False))
    return 1 if total["errors"] else 0


def find_lead_for_disk_folder(amo: AmoClient, folder_name: str, allowed: dict[int, str]) -> dict[str, Any] | None:
    candidate_ids = folder_candidate_ids(folder_name)
    if not candidate_ids:
        return None

    for candidate_id in candidate_ids:
        try:
            lead = amo.get_lead(candidate_id)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                continue
            raise
        if int(lead.get("pipeline_id") or 0) in allowed:
            return lead

    return None


def folder_candidate_ids(folder_name: str) -> list[int]:
    result: list[int] = []
    for match in re.finditer(r"(?<!\d)(\d{6,10})(?!\d)", folder_name):
        value = int(match.group(1))
        if value not in result:
            result.append(value)
    return result


def folder_search_query(folder_name: str) -> str:
    value = re.sub(r"\(\s*\d{6,10}\s*\)", " ", folder_name)
    value = re.sub(r"\b\d{6,10}\b", " ", value)
    value = re.sub(r"[_\-]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:120]


def normalize_match_text(value: str) -> str:
    value = value.lower()
    value = re.sub(r"\(\s*\d{6,10}\s*\)", " ", value)
    value = re.sub(r"\b\d{6,10}\b", " ", value)
    value = re.sub(r"[_\-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def command_scan_disk_folders(args: argparse.Namespace) -> int:
    return scan_disk_folders_once(args, AmoClient(), YandexDiskClient(), Store())


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync amoCRM files and attachment notes to Yandex Disk.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="Do not create folders or upload files.")
    common.add_argument("--force", action="store_true", help="Upload even if local state says the file was uploaded.")
    common.add_argument("--include-audio", action="store_true", help="Also upload audio files.")
    common.add_argument("--no-contact-notes", action="store_true", help="Do not inspect linked contact attachment notes.")

    test_last = subparsers.add_parser("test-last", parents=[common], help="Upload attachments from latest CRM leads to test folders.")
    test_last.add_argument("--limit", type=int, default=3)
    test_last.set_defaults(func=command_test_last)

    test_leads = subparsers.add_parser("test-leads", parents=[common], help="Upload attachments from selected CRM leads to test folders.")
    test_leads.add_argument("--lead-id", type=int, action="append", required=True)
    test_leads.set_defaults(func=command_test_leads)

    sync_field_leads = subparsers.add_parser(
        "sync-field-leads",
        parents=[common],
        help="Upload selected CRM leads to folders from the Yandex Disk field.",
    )
    sync_field_leads.add_argument("--lead-id", type=int, action="append", required=True)
    sync_field_leads.set_defaults(func=command_sync_field_leads)

    sync_field = subparsers.add_parser("sync-by-field", parents=[common], help="Upload attachments to folders from a lead custom field.")
    sync_field.add_argument("--all", action="store_true", help="Scan all leads instead of only leads updated since the last run.")
    sync_field.add_argument("--since", help="ISO date/datetime for updated_at lower bound, for example 2026-06-01.")
    sync_field.add_argument("--limit", type=int, default=0, help="Stop after this many leads with filled folder field.")
    sync_field.set_defaults(func=command_sync_by_field)

    sync_events = subparsers.add_parser("sync-field-events", parents=[common], help="Process Yandex Disk field change events once.")
    sync_events.add_argument("--since", help="ISO date/datetime lower bound, for example 2026-06-01T00:00:00.")
    sync_events.add_argument("--lookback-hours", type=float, default=24.0)
    sync_events.set_defaults(func=command_sync_field_events)

    monitor_events = subparsers.add_parser("monitor-field-events", parents=[common], help="Continuously process Yandex Disk field change events.")
    monitor_events.add_argument("--since", help="ISO date/datetime lower bound for the first pass.")
    monitor_events.add_argument("--lookback-hours", type=float, default=24.0)
    monitor_events.add_argument("--interval-seconds", type=int, default=300)
    monitor_events.set_defaults(func=command_monitor_field_events)

    scan_disk = subparsers.add_parser("scan-disk-folders", parents=[common], help="Find Yandex Disk folders, fill CRM field, and sync files.")
    scan_disk.set_defaults(func=command_scan_disk_folders)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
