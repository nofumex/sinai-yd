from __future__ import annotations

import argparse
import atexit
import contextlib
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

import sync_crm_files_to_yandex as sync


LOG_DIR = Path(__file__).resolve().parent / "data" / "logs"
LOCK_PATH = Path(__file__).resolve().parent / "data" / "main.lock"
PENDING_KEY = "telegram_pending_batch"
PENDING_PROCESSING_KEY = "telegram_pending_processing"
PANEL_MESSAGE_KEY = "telegram_panel_message_id"
TG_OFFSET_KEY = "telegram_update_offset"
MONITOR_ENABLED_KEY = "monitor_enabled"


LOCAL_TZ = timezone(timedelta(hours=int(sync.env("LOCAL_TZ_OFFSET_HOURS", "7"))))


@dataclass
class LastCheck:
    checked_at: str = "-"
    period: str = "-"
    field_events_seen: int = 0
    field_events_new: int = 0
    disk_folders_seen: int = 0
    disk_folders_new: int = 0
    processed_tasks: int = 0
    uploaded: int = 0
    skipped: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class AppState:
    started_at: str
    last_check: LastCheck = field(default_factory=LastCheck)
    cycles: int = 0
    total_uploaded: int = 0
    total_errors: int = 0
    last_error: str = ""


class TelegramUI:
    def __init__(self, store: sync.Store) -> None:
        self.store = store
        self.token = sync.env("TG_BOT_TOKEN")
        self.admin_id = sync.env("ADMIN_ID")
        self.enabled = bool(self.token and self.admin_id)
        self.session = requests.Session()
        self.base_url = f"https://api.telegram.org/bot{self.token}" if self.enabled else ""

    def api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return {}
        response = self.session.post(f"{self.base_url}/{method}", json=payload, timeout=45)
        if response.status_code >= 400:
            raise RuntimeError(response.text)
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data)
        return data.get("result") or {}

    def get_updates(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        offset = self.store.get_setting_int(TG_OFFSET_KEY, 0)
        try:
            response = self.session.get(
                f"{self.base_url}/getUpdates",
                params={"offset": offset, "timeout": 1},
                timeout=20,
            )
            response.raise_for_status()
            updates = response.json().get("result") or []
            if updates:
                self.store.set_setting_int(TG_OFFSET_KEY, int(updates[-1]["update_id"]) + 1)
            return updates
        except Exception as exc:
            write_log(f"Telegram getUpdates failed, continuing: {exc}")
            return []

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self.api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
        except Exception as exc:
            write_log(f"answerCallbackQuery failed: {exc}")

    def panel_message_id(self) -> int:
        return self.store.get_setting_int(PANEL_MESSAGE_KEY, 0)

    def send_panel(self, text: str, keyboard: list[list[dict[str, str]]]) -> int:
        result = self.api(
            "sendMessage",
            {
                "chat_id": self.admin_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )
        message_id = int(result.get("message_id") or 0)
        if message_id:
            self.store.set_setting_int(PANEL_MESSAGE_KEY, message_id)
        return message_id

    def edit_panel(self, text: str, keyboard: list[list[dict[str, str]]]) -> None:
        if not self.enabled:
            return
        message_id = self.panel_message_id()
        if not message_id:
            self.send_panel(text, keyboard)
            return
        try:
            self.api(
                "editMessageText",
                {
                    "chat_id": self.admin_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": {"inline_keyboard": keyboard},
                },
            )
        except Exception as exc:
            error_text = str(exc)
            if "message is not modified" in error_text:
                return
            # If Telegram cannot edit an old/deleted message, delete what we know
            # and recreate exactly one current panel.
            write_log(f"edit panel failed, recreating once: {exc}")
            self.delete_message(message_id)
            self.store.delete_setting(PANEL_MESSAGE_KEY)
            self.send_panel(text, keyboard)

    def delete_message(self, message_id: int) -> None:
        try:
            self.api("deleteMessage", {"chat_id": self.admin_id, "message_id": message_id})
        except Exception:
            pass


def html(value: Any) -> str:
    text = str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_log(text: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"main_{now_dt().strftime('%Y-%m-%d')}.log"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(sanitize_log_text(text).rstrip() + "\n")


def trace_log(text: str) -> None:
    write_log(text)
    print(sanitize_log_text(text), flush=True)


def sanitize_log_text(text: str) -> str:
    text = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***", str(text))
    replacements = [
        sync.env("TG_BOT_TOKEN"),
        sync.env("AMOCRM_ACCESS_TOKEN"),
        sync.env("YANDEX_DISK_TOKEN"),
        sync.env("OPENAI_API_KEY"),
        sync.env("GROQ_API_KEY"),
        sync.env("FREELLM_API_KEY"),
    ]
    for secret in replacements:
        if secret:
            text = text.replace(secret, "***")
    return text


def acquire_lock() -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            old_pid = 0
        if old_pid and process_exists(old_pid):
            raise RuntimeError(f"main.py is already running with pid {old_pid}")
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(release_lock)


def release_lock() -> None:
    try:
        if LOCK_PATH.exists() and LOCK_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except Exception:
        pass


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def monitor_enabled(store: sync.Store) -> bool:
    return store.get_setting(MONITOR_ENABLED_KEY, "1") != "0"


def set_monitor_enabled(store: sync.Store, enabled: bool) -> None:
    store.set_setting(MONITOR_ENABLED_KEY, "1" if enabled else "0")


def pending_batch(store: sync.Store) -> dict[str, Any] | None:
    raw = store.get_setting(PENDING_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        store.delete_setting(PENDING_KEY)
        return None


def save_pending_batch(store: sync.Store, batch: dict[str, Any]) -> None:
    store.set_setting(PENDING_KEY, json.dumps(batch, ensure_ascii=False))


def clear_pending_batch(store: sync.Store) -> None:
    store.delete_setting(PENDING_KEY)
    store.delete_setting(PENDING_PROCESSING_KEY)


def base_worker_args(cli: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        dry_run=cli.dry_run,
        force=False,
        include_audio=False,
        no_contact_notes=False,
        since=None,
        lookback_hours=cli.lookback_hours,
        interval_seconds=cli.interval_seconds,
    )


def discover_event_tasks(args: argparse.Namespace, amo: sync.AmoClient, store: sync.Store) -> tuple[list[dict[str, Any]], int, str]:
    field_id = amo.resolve_folder_field_id()
    now = int(time.time())
    setting_key = f"last_event_created_at_field_{field_id}"
    saved_ts = store.get_setting_int(setting_key, 0)
    fallback = now - int(args.lookback_hours * 3600)
    from_ts = max(0, saved_ts - 60) if saved_ts else fallback
    trace_log(f"event scan start field_id={field_id} from={fmt_time(from_ts)} to={fmt_time(now)}")
    events = amo.list_field_change_events(field_id, from_ts=from_ts, to_ts=now)
    tasks = [{"kind": "event", "event": event} for event in events if not store.has_event(str(event.get("id") or ""))]
    period = f"{fmt_time(from_ts)} - {fmt_time(now)}"
    trace_log(f"event scan done seen={len(events)} new={len(tasks)}")
    return tasks, len(events), period


def discover_disk_tasks(amo: sync.AmoClient, disk: sync.YandexDiskClient, store: sync.Store) -> tuple[list[dict[str, Any]], int]:
    allowed = amo.allowed_pipeline_ids()
    root = sync.normalize_disk_path(sync.env("YANDEX_DISK_SCAN_ROOT", sync.env("YANDEX_DISK_TEST_ROOT", "test-CRM")))
    trace_log(f"disk scan start root=disk:/{root}")
    folders = [item for item in disk.list_dir(root) if item.get("type") == "dir"]
    trace_log(f"disk scan listed folders={len(folders)} root=disk:/{root}")
    tasks: list[dict[str, Any]] = []
    without_id = 0
    for index, folder in enumerate(folders, start=1):
        folder_name = str(folder.get("name") or "")
        folder_path = str(folder.get("path") or "").replace("disk:/", "", 1)
        if not folder_path or store.has_disk_folder(folder_path):
            continue
        if not sync.folder_candidate_ids(folder_name):
            without_id += 1
            continue
        folder_modified = str(folder.get("modified") or folder.get("created") or "")
        if store.has_disk_folder_no_match(folder_path, folder_modified):
            continue
        if index == 1 or index % 10 == 0 or index == len(folders):
            trace_log(f"disk scan progress checked={index}/{len(folders)} new={len(tasks)}")
        lead = sync.find_lead_for_disk_folder(amo, folder_name, allowed)
        if not lead:
            store.save_disk_folder_no_match(folder_path, folder_name, folder_modified)
            continue
        tasks.append({"kind": "disk_folder", "folder_name": folder_name, "folder_path": folder_path, "lead_id": int(lead["id"])})
    trace_log(f"disk scan done folders={len(folders)} without_id={without_id} new={len(tasks)}")
    return tasks, len(folders)


def process_task(
    task: dict[str, Any],
    args: argparse.Namespace,
    amo: sync.AmoClient,
    disk: sync.YandexDiskClient,
    store: sync.Store,
) -> dict[str, int | str]:
    if task["kind"] == "event":
        return process_event_task(task["event"], args, amo, disk, store)
    if task["kind"] == "disk_folder":
        return process_disk_folder_task(task, args, amo, disk, store)
    raise ValueError(f"Unknown task kind: {task.get('kind')}")


def process_task_logged(
    task: dict[str, Any],
    args: argparse.Namespace,
    amo: sync.AmoClient,
    disk: sync.YandexDiskClient,
    store: sync.Store,
) -> dict[str, int | str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        result = process_task(task, args, amo, disk, store)
    output = buffer.getvalue().strip()
    if output:
        write_log(output)
    return result


def process_event_task(event: dict[str, Any], args: argparse.Namespace, amo: sync.AmoClient, disk: sync.YandexDiskClient, store: sync.Store) -> dict[str, int | str]:
    field_id = amo.resolve_folder_field_id()
    allowed = amo.allowed_pipeline_ids()
    lead_id = int(event.get("entity_id") or 0)
    if not lead_id:
        return {"uploaded": 0, "skipped": 1, "errors": 0, "label": "event without lead"}
    lead = amo.get_lead(lead_id)
    if int(lead.get("pipeline_id") or 0) not in allowed:
        if not args.dry_run:
            store.save_event(event)
        return {"uploaded": 0, "skipped": 1, "errors": 0, "label": f"lead {lead_id} skipped by pipeline"}
    raw_folder = sync.lead_folder_value(lead, field_id) or sync.event_folder_value(event, field_id)
    if not raw_folder:
        if not args.dry_run:
            store.save_event(event)
        return {"uploaded": 0, "skipped": 1, "errors": 0, "label": f"lead {lead_id} empty folder"}
    stats = sync.sync_lead(
        amo,
        disk,
        store,
        lead,
        sync.crm_files_folder(raw_folder),
        force=args.force,
        dry_run=args.dry_run,
        include_contact_notes=not args.no_contact_notes,
        skip_audio=not args.include_audio,
    )
    if not args.dry_run and stats["errors"] == 0:
        store.save_event(event)
    return {"uploaded": stats["uploaded"], "skipped": stats["skipped"], "errors": stats["errors"], "label": f"field event lead {lead_id}"}


def process_disk_folder_task(task: dict[str, Any], args: argparse.Namespace, amo: sync.AmoClient, disk: sync.YandexDiskClient, store: sync.Store) -> dict[str, int | str]:
    field_id = amo.resolve_folder_field_id()
    lead_id = int(task["lead_id"])
    folder_path = str(task["folder_path"])
    lead = amo.get_lead(lead_id)
    field_value = sync.yandex_client_url(folder_path)
    current_value = sync.lead_folder_value(lead, field_id)
    updated = 0
    if not current_value or sync.normalize_disk_path(current_value) != sync.normalize_disk_path(field_value):
        if not args.dry_run:
            amo.update_lead_folder_field(lead_id, field_id, field_value)
        updated = 1
    stats = sync.sync_lead(
        amo,
        disk,
        store,
        lead,
        sync.crm_files_folder(folder_path),
        force=args.force,
        dry_run=args.dry_run,
        include_contact_notes=not args.no_contact_notes,
        skip_audio=not args.include_audio,
    )
    if not args.dry_run and stats["errors"] == 0:
        store.save_disk_folder(folder_path, lead_id, field_value)
    return {"uploaded": stats["uploaded"], "skipped": stats["skipped"], "errors": stats["errors"], "updated": updated, "label": f"disk folder lead {lead_id}"}


def run_monitor_cycle(
    args: argparse.Namespace,
    amo: sync.AmoClient,
    disk: sync.YandexDiskClient,
    store: sync.Store,
    state: AppState,
) -> None:
    check = LastCheck(checked_at=now_str())
    state.cycles += 1
    trace_log(f"cycle {state.cycles} start dry_run={args.dry_run}")

    if pending_batch(store):
        check.notes.append("Есть ожидающая подтверждения пачка, новые задачи не запускаются.")
        state.last_check = check
        return

    event_tasks, event_seen, period = discover_event_tasks(args, amo, store)
    disk_tasks, disk_seen = discover_disk_tasks(amo, disk, store)
    check.period = period
    check.field_events_seen = event_seen
    check.field_events_new = len(event_tasks)
    check.disk_folders_seen = disk_seen
    check.disk_folders_new = len(disk_tasks)

    tasks = event_tasks + disk_tasks
    if not tasks:
        state.last_check = check
        trace_log(f"cycle {state.cycles} done no tasks event_seen={event_seen} disk_seen={disk_seen}")
        return

    too_many_events = len(event_tasks) > 5
    too_many_disk = len(disk_tasks) > 5
    if too_many_events or too_many_disk:
        first, rest = tasks[0], tasks[1:]
        result = process_task_logged(first, args, amo, disk, store)
        apply_result(check, result)
        batch = {
            "created_at": now_str(),
            "field_events": len(event_tasks),
            "disk_folders": len(disk_tasks),
            "done_first": task_public_links(first),
            "tasks": rest,
        }
        save_pending_batch(store, batch)
        check.notes.append(
            f"Большая пачка: изменений поля {len(event_tasks)}, новых папок Диска {len(disk_tasks)}. "
            f"Сделана 1 задача, остальные ждут подтверждения."
        )
    else:
        for task in tasks:
            result = process_task_logged(task, args, amo, disk, store)
            apply_result(check, result)

    state.total_uploaded += check.uploaded
    state.total_errors += check.errors
    if check.errors:
        state.last_error = "; ".join(check.notes[-3:]) or "Есть ошибки в последнем цикле"
    state.last_check = check
    trace_log(f"cycle {state.cycles} done tasks={check.processed_tasks} uploaded={check.uploaded} skipped={check.skipped} errors={check.errors}")


def apply_result(check: LastCheck, result: dict[str, int | str]) -> None:
    check.processed_tasks += 1
    check.uploaded += int(result.get("uploaded") or 0)
    check.skipped += int(result.get("skipped") or 0)
    check.errors += int(result.get("errors") or 0)
    label = str(result.get("label") or "")
    if label:
        check.notes.append(label)


def process_pending_batch(args: argparse.Namespace, amo: sync.AmoClient, disk: sync.YandexDiskClient, store: sync.Store, state: AppState) -> LastCheck:
    batch = pending_batch(store)
    check = LastCheck(checked_at=now_str(), period="pending batch")
    if not batch:
        check.notes.append("Нет ожидающей пачки.")
        return check
    for task in batch.get("tasks") or []:
        result = process_task_logged(task, args, amo, disk, store)
        apply_result(check, result)
    clear_pending_batch(store)
    state.total_uploaded += check.uploaded
    state.total_errors += check.errors
    state.last_check = check
    return check


def skip_pending_batch(store: sync.Store) -> None:
    batch = pending_batch(store)
    if not batch:
        return
    for task in batch.get("tasks") or []:
        if task.get("kind") == "event":
            store.save_event(task["event"])
        elif task.get("kind") == "disk_folder":
            store.save_disk_folder(str(task["folder_path"]), int(task["lead_id"]), sync.yandex_client_url(str(task["folder_path"])))
    clear_pending_batch(store)


def task_public_links(task: dict[str, Any]) -> dict[str, str]:
    if task.get("kind") == "event":
        event = task.get("event") or {}
        lead_id = int(event.get("entity_id") or 0)
        disk_value = ""
        for value in event.get("value_after") or []:
            custom = value.get("custom_field_value") or {}
            disk_value = str(custom.get("text") or custom.get("value") or "").strip()
            if disk_value:
                break
        try:
            disk_link = disk_value if disk_value.startswith("http") else sync.yandex_client_url(sync.normalize_disk_path(disk_value))
        except Exception:
            disk_link = disk_value
        return {"crm": f"{sync.env('AMOCRM_BASE_URL').rstrip('/')}/leads/detail/{lead_id}", "disk": disk_link}
    if task.get("kind") == "disk_folder":
        return {
            "crm": f"{sync.env('AMOCRM_BASE_URL').rstrip('/')}/leads/detail/{int(task.get('lead_id') or 0)}",
            "disk": sync.yandex_client_url(str(task.get("folder_path") or "")),
        }
    return {"crm": "", "disk": ""}


def render_panel(state: AppState, store: sync.Store, args: argparse.Namespace, view: str = "monitor") -> tuple[str, list[list[dict[str, str]]]]:
    enabled = monitor_enabled(store)
    pending = pending_batch(store)
    pending_processing = store.get_setting(PENDING_PROCESSING_KEY)
    check = state.last_check
    status = "включен" if enabled else "выключен"
    text = [
        "📡 <b>Мониторинг CRM → Яндекс.Диск</b>",
        f"Статус: <b>{html(status)}</b>",
        f"Интервал: каждые <b>{args.interval_seconds}</b> сек",
        f"Окно проверки событий: последние <b>{args.lookback_hours:g}</b> ч",
        "",
        "<b>Последний чек</b>",
        f"Время: {html(check.checked_at)}",
        f"Период событий: {html(check.period)}",
        f"Событий поля найдено: <b>{check.field_events_seen}</b>",
        f"Новых событий поля: <b>{check.field_events_new}</b>",
        f"Папок на Диске просмотрено: <b>{check.disk_folders_seen}</b>",
        f"Новых папок к обработке: <b>{check.disk_folders_new}</b>",
        f"Задач обработано: <b>{check.processed_tasks}</b>",
        f"Файлов загружено: <b>{check.uploaded}</b>",
        f"Пропущено/уже было: <b>{check.skipped}</b>",
        f"Ошибок: <b>{check.errors}</b>",
        "",
        "<b>Итого за запуск</b>",
        f"Циклов: <b>{state.cycles}</b>",
        f"Файлов загружено: <b>{state.total_uploaded}</b>",
        f"Ошибок: <b>{state.total_errors}</b>",
    ]
    if pending:
        done = pending.get("done_first") or {}
        text.extend(
            [
                "",
                "⚠️ <b>Нужное подтверждение</b>",
                f"Новых изменений поля: <b>{pending.get('field_events', 0)}</b>",
                f"Новых папок на Диске: <b>{pending.get('disk_folders', 0)}</b>",
                f"Осталось задач: <b>{len(pending.get('tasks') or [])}</b>",
                "1 задача уже выполнена.",
            ]
        )
        if done.get("crm"):
            text.append(f"CRM: {html(done['crm'])}")
        if done.get("disk"):
            text.append(f"Диск: {html(done['disk'])}")
        if pending_processing == "yes":
            text.append("✅ Подтверждение принято. Обрабатываю оставшиеся задачи...")
        elif pending_processing == "no":
            text.append("❌ Решение принято. Пачка будет пропущена.")
        else:
            text.append("Пробросить остальное?")
    if check.notes:
        text.extend(["", "<b>Детали</b>"])
        text.extend(f"• {html(note)}" for note in check.notes[-8:])

    keyboard = [
        [{"text": "🔄 Обновить", "callback_data": "panel:refresh"}, {"text": "⏯ Старт/стоп", "callback_data": "monitor:toggle"}],
    ]
    if pending and not pending_processing:
        keyboard.insert(0, [{"text": "✅ Да, пробросить остальное", "callback_data": "pending:yes"}])
        keyboard.insert(1, [{"text": "❌ Нет, пропустить", "callback_data": "pending:no"}])
    keyboard.append([{"text": "📡 Мониторинг", "callback_data": "view:monitor"}, {"text": "🏠 Главное меню", "callback_data": "view:main"}])

    if view == "main":
        text.insert(0, "🏠 <b>Главное меню</b>\n")
    return "\n".join(text), keyboard


def handle_updates(
    tg: TelegramUI,
    args: argparse.Namespace,
    amo: sync.AmoClient,
    disk: sync.YandexDiskClient,
    store: sync.Store,
    state: AppState,
) -> None:
    for update in tg.get_updates():
        message = update.get("message") or {}
        callback = update.get("callback_query") or {}
        user_id = int(((message.get("from") or callback.get("from") or {}).get("id") or 0))
        if str(user_id) != str(sync.env("ADMIN_ID")):
            continue

        if message:
            text = str(message.get("text") or "")
            if text.startswith("/start") or text.startswith("/panel"):
                tg.delete_message(int(message.get("message_id") or 0))
                panel_text, keyboard = render_panel(state, store, args, view="main")
                tg.edit_panel(panel_text, keyboard)
            continue

        if not callback:
            continue
        data = str(callback.get("data") or "")
        tg.answer_callback(str(callback.get("id") or ""))
        if data == "monitor:toggle":
            set_monitor_enabled(store, not monitor_enabled(store))
        elif data == "pending:yes":
            store.set_setting(PENDING_PROCESSING_KEY, "yes")
            panel_text, keyboard = render_panel(state, store, args, view="monitor")
            tg.edit_panel(panel_text, keyboard)
            check = process_pending_batch(args, amo, disk, store, state)
            write_log("Pending batch processed\n" + json.dumps(check.__dict__, ensure_ascii=False))
        elif data == "pending:no":
            store.set_setting(PENDING_PROCESSING_KEY, "no")
            panel_text, keyboard = render_panel(state, store, args, view="monitor")
            tg.edit_panel(panel_text, keyboard)
            skip_pending_batch(store)
            store.delete_setting(PENDING_PROCESSING_KEY)
            state.last_check.notes.append("Ожидающая пачка пропущена админом.")
        panel_text, keyboard = render_panel(state, store, args, view="main" if data == "view:main" else "monitor")
        tg.edit_panel(panel_text, keyboard)


def fmt_time(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def now_dt() -> datetime:
    return datetime.now(LOCAL_TZ)


def now_str() -> str:
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def html(value: Any) -> str:
    text = str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run amoCRM -> Yandex Disk monitor.")
    parser.add_argument("--interval-seconds", type=int, default=int(sync.env("MONITOR_INTERVAL_SECONDS", "300")))
    parser.add_argument("--lookback-hours", type=float, default=float(sync.env("EVENT_LOOKBACK_HOURS", "24")))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--reset-panel", action="store_true", help="Delete the known panel message and create a fresh one.")
    parser.add_argument("--approve-pending", action="store_true", help="Process the pending batch and exit.")
    parser.add_argument("--reject-pending", action="store_true", help="Mark the pending batch as skipped and exit.")
    args = parser.parse_args()
    worker_args = base_worker_args(args)

    acquire_lock()
    amo = sync.AmoClient()
    disk = sync.YandexDiskClient()
    store = sync.Store()
    tg = TelegramUI(store)
    state = AppState(started_at=now_str())

    if args.approve_pending:
        store.set_setting(PENDING_PROCESSING_KEY, "yes")
        panel_text, keyboard = render_panel(state, store, worker_args, view="monitor")
        tg.edit_panel(panel_text, keyboard)
        check = process_pending_batch(worker_args, amo, disk, store, state)
        write_log("Pending batch processed from CLI\n" + json.dumps(check.__dict__, ensure_ascii=False))
        panel_text, keyboard = render_panel(state, store, worker_args, view="monitor")
        tg.edit_panel(panel_text, keyboard)
        return 0 if check.errors == 0 else 1

    if args.reject_pending:
        store.set_setting(PENDING_PROCESSING_KEY, "no")
        panel_text, keyboard = render_panel(state, store, worker_args, view="monitor")
        tg.edit_panel(panel_text, keyboard)
        skip_pending_batch(store)
        state.last_check.notes.append("Ожидающая пачка пропущена из CLI.")
        panel_text, keyboard = render_panel(state, store, worker_args, view="monitor")
        tg.edit_panel(panel_text, keyboard)
        return 0

    if args.reset_panel:
        old_id = tg.panel_message_id()
        if old_id:
            tg.delete_message(old_id)
        store.delete_setting(PANEL_MESSAGE_KEY)

    write_log(f"Monitor started interval={args.interval_seconds}s dry_run={args.dry_run}")
    panel_text, keyboard = render_panel(state, store, worker_args, view="main")
    tg.edit_panel(panel_text, keyboard)

    while True:
        handle_updates(tg, worker_args, amo, disk, store, state)
        if monitor_enabled(store):
            try:
                state.last_check = LastCheck(
                    checked_at=now_str(),
                    notes=["⏳ Идет проверка и синхронизация..."],
                )
                panel_text, keyboard = render_panel(state, store, worker_args, view="monitor")
                tg.edit_panel(panel_text, keyboard)
                run_monitor_cycle(worker_args, amo, disk, store, state)
            except Exception as exc:
                state.total_errors += 1
                state.last_error = str(exc)
                state.last_check = LastCheck(checked_at=now_str(), errors=1, notes=[f"fatal cycle error: {exc}"])
                write_log(f"fatal cycle error: {exc}")

        panel_text, keyboard = render_panel(state, store, worker_args, view="monitor")
        tg.edit_panel(panel_text, keyboard)
        write_log(panel_text)
        if args.once:
            return 0

        deadline = time.time() + args.interval_seconds
        while time.time() < deadline:
            handle_updates(tg, worker_args, amo, disk, store, state)
            time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
