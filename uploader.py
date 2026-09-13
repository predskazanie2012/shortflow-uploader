from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Locator, Page, Playwright


PlaywrightTimeoutError = TimeoutError
sync_playwright = None
LAUNCHED_CHROME_PROCESS: subprocess.Popen | None = None


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"


def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


configure_console_encoding()


CHROME_LAUNCH_ARGS = [
    "--start-maximized",
    "--disable-quic",
    "--disable-features=UseDnsHttpsSvcb",
]
LOGIN_CHROME_CDP_PORT = 9223
CHANNEL_FOLDER_PREFIXES = {
    "en": ("english",),
    "fr": ("french",),
    "es": ("spanish",),
    "pt": ("portuguese",),
    "ru": ("russian",),
    "de": ("german",),
    "zh": ("chinese",),
    "ja": ("japanese",),
    "ar": ("arabic",),
    "hi": ("hindi",),
}


def chrome_launch_args(start_url: str | None = None) -> list[str]:
    args = list(CHROME_LAUNCH_ARGS)
    if start_url:
        args.append(start_url)
    return args


def chrome_executable_path() -> str:
    candidates = [
        shutil.which("chrome.exe"),
        shutil.which("chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise RuntimeError("Google Chrome was not found on this computer")


def reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_cdp_endpoint(port: int, timeout_seconds: float = 30.0) -> None:
    deadline = time.time() + timeout_seconds
    url = f"http://127.0.0.1:{port}/json/version"
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            time.sleep(0.5)
    raise RuntimeError(f"Chrome remote debugging endpoint did not start on port {port}") from last_error


def detect_channel_key_from_folder(video_folder: Path) -> str | None:
    for part in (video_folder, *video_folder.parents[:3]):
        name = part.name.strip().lower()
        if not name or name == "clips_final":
            continue
        for channel_key, prefixes in CHANNEL_FOLDER_PREFIXES.items():
            if any(name == prefix or name.startswith(f"{prefix}_") for prefix in prefixes):
                return channel_key
    return None


def validate_channel_folder_alignment(video_folder: Path, active_channel: str) -> None:
    detected_channel = detect_channel_key_from_folder(video_folder)
    if detected_channel is None or active_channel == "default":
        return
    if detected_channel != active_channel:
        raise ValueError(
            "Safety check failed: source folder language does not match the selected channel "
            f"({detected_channel} folder vs {active_channel} channel)."
        )


def try_connect_existing_chrome(
    playwright: Playwright,
    timeout_seconds: float = 1.5,
) -> tuple[BrowserContext, Browser] | None:
    try:
        wait_for_cdp_endpoint(LOGIN_CHROME_CDP_PORT, timeout_seconds=timeout_seconds)
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{LOGIN_CHROME_CDP_PORT}")
        context = browser.contexts[0] if browser.contexts else browser.new_context(viewport=None)
    except Exception:
        return None
    return context, browser


UI_TEXT = {
    "create": [
        "Create",
        "Создать",
        "Vytvoriť",
        "Créer",
        "Crear",
        "Erstellen",
        "Criar",
    ],
    "upload_videos": [
        "Upload videos",
        "Upload video",
        "Add video",
        "Загрузить видео",
        "Добавить видео",
        "Nahrať video",
        "Nahrať videá",
        "Pridať video",
        "Pridať videá",
        "Importer des vidéos",
        "Importer une vidéo",
        "Mettre en ligne des vidéos",
        "Mettre en ligne une vidéo",
        "Ajouter une vidéo",
        "Ajouter des vidéos",
        "Subir vídeos",
        "Subir vídeo",
        "Subir videos",
        "Subir video",
        "Añadir vídeo",
        "Añadir vídeos",
        "Añadir video",
        "Añadir videos",
        "Agregar vídeo",
        "Agregar video",
        "Videos hochladen",
        "Video hochladen",
        "Video hinzufügen",
        "Enviar vídeos",
        "Enviar vídeo",
        "Enviar videos",
        "Enviar video",
        "Carregar vídeos",
        "Carregar vídeo",
        "Adicionar vídeo",
        "Adicionar vídeos",
        "Adicionar video",
        "Adicionar videos",
    ],
    "next": [
        "Next",
        "Далее",
        "Продолжить",
        "Ďalej",
        "Pokračovať",
        "Suivant",
        "Continuer",
        "Siguiente",
        "Continuar",
        "Weiter",
        "Avançar",
        "Próxima",
        "Continuar",
    ],
    "save": [
        "Save",
        "Сохранить",
        "Uložiť",
        "Enregistrer",
        "Guardar",
        "Speichern",
        "Salvar",
    ],
    "publish": [
        "Publish",
        "Опубликовать",
        "Zverejniť",
        "Publikovať",
        "Publier",
        "Publicar",
        "Veröffentlichen",
    ],
    "schedule": [
        "Schedule",
        "Запланировать",
        "Naplánovať",
        "Programmer",
        "Programar",
        "Planen",
        "Agendar",
    ],
    "made_for_kids_no": [
        "No, it's not made for kids",
        "No, it’s not made for kids",
        "No, this video is not made for kids",
        "Нет, это видео не для детей",
        "Нет, это видео не предназначено для детей",
        "Nie, toto video nie je určené pre deti",
        "Nie, toto video nie je vytvorené pre deti",
        "Nie, nie je určené pre deti",
        "Nie, nie je pre deti",
        "Non, elle n'est pas conçue pour les enfants",
        "Non, cette vidéo n'est pas destinée aux enfants",
        "Non, ce n'est pas conçu pour les enfants",
        "No, no es contenido creado para niños",
        "No, no está creado para niños",
        "No, no es para niños",
        "Nein, es ist nicht speziell für Kinder",
        "Nein, dieses Video ist nicht speziell für Kinder",
        "Nein, dieses Video ist nicht für Kinder",
        "Não, não é conteúdo para crianças",
        "Não, este vídeo não é destinado a crianças",
        "Não, não é para crianças",
    ],
    "visibility_private": [
        "Private",
        "Приватный",
        "Закрытый доступ",
        "Ограниченный доступ",
        "Súkromné",
        "Súkromný",
        "Privée",
        "Privado",
        "Privat",
    ],
    "visibility_unlisted": [
        "Unlisted",
        "Доступ по ссылке",
        "Neuvedené",
        "Nezaradené",
        "Non répertoriée",
        "No listado",
        "No listada",
        "Nicht gelistet",
        "Não listado",
        "Não listada",
    ],
    "visibility_public": [
        "Public",
        "Открытый доступ",
        "Verejné",
        "Verejný",
        "Publique",
        "Público",
        "Publico",
        "Öffentlich",
    ],
    "close": [
        "Close",
        "Закрыть",
        "Zavrieť",
        "Fermer",
        "Cerrar",
        "Schließen",
        "Fechar",
    ],
    "discard": [
        "Discard",
        "Discard changes",
        "Удалить",
        "Отменить изменения",
        "Zahodiť",
        "Zahodiť zmeny",
        "Zrušiť zmeny",
        "Ignorer",
        "Descartar",
        "Verwerfen",
        "Descartar alterações",
    ],
    "final_success": [
        "Video published",
        "Your video has been published",
        "Video uploaded",
        "Video scheduled",
        "Your video is scheduled",
        "Video has been scheduled",
        "will be published",
        "\u0412\u0438\u0434\u0435\u043e \u043e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e",
        "\u0412\u0438\u0434\u0435\u043e \u0437\u0430\u0433\u0440\u0443\u0436\u0435\u043d\u043e",
        "\u0412\u0438\u0434\u0435\u043e \u0437\u0430\u043f\u043b\u0430\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u043e",
        "\u041f\u0443\u0431\u043b\u0438\u043a\u0430\u0446\u0438\u044f \u0432\u0438\u0434\u0435\u043e \u0437\u0430\u043f\u043b\u0430\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u0430",
        "\u0431\u0443\u0434\u0435\u0442 \u043e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e",
        "video publiee",
        "video publicado",
        "video publicada",
        "video veroffentlicht",
        "video zverejnene",
        "video bolo zverejnene",
        "Публикация видео запланирована",
        "Видео запланировано",
        "будет опубликовано",
        "Publication programmée",
        "Vidéo programmée",
        "sera publiée",
        "Video programado",
        "Vídeo programado",
        "se publicará",
        "Video geplant",
        "wird veröffentlicht",
        "Naplánované",
        "Video bolo naplánované",
        "bude zverejnené",
    ],
}


METADATA_SUFFIXES = [".txt", ".json"]
METADATA_NAME_PATTERNS = [
    "{stem}_youtube.txt",
    "{stem}.txt",
    "{stem}_meta.json",
    "{stem}_youtube.json",
    "{stem}.json",
]


VISIBILITY_TO_RADIO_NAME = {
    "private": "PRIVATE",
    "unlisted": "UNLISTED",
    "public": "PUBLIC",
}

SCHEDULE_SLOT_OFFSETS = {
    "manual": 0,
    "en": 0,
    "fr": 15,
    "es": 30,
    "de": 45,
    "ru": 60,
    "pt": 75,
    "ja": 90,
    "jp": 90,
    "hi": 105,
    "ar": 120,
    "zh": 135,
    "cn": 135,
}
SCHEDULE_GROUP_STEP_MINUTES = 15

UPLOAD_UNAVAILABLE_MARKERS = [
    "upload unavailable",
    "uploading is unavailable",
    "upload not available",
    "you can upload this video in 24 hours",
    "try again in 24 hours",
    "nahravanie nie je dostupne",
    "nahravanie nie je k dispozicii",
    "nahratie nie je k dispozicii",
    "zavrsit overenie",
    "\u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0430 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430",
    "\u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u044d\u0442\u043e \u0432\u0438\u0434\u0435\u043e \u043c\u043e\u0436\u043d\u043e \u0431\u0443\u0434\u0435\u0442 \u0447\u0435\u0440\u0435\u0437 24",
    "\u043f\u0440\u043e\u0439\u0434\u0438\u0442\u0435 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0443",
    "carga no disponible",
    "subida no disponible",
    "importation indisponible",
    "mise en ligne indisponible",
    "upload nicht verfugbar",
    "hochladen nicht verfugbar",
    "upload indisponivel",
    "envio indisponivel",
]


@dataclass(frozen=True)
class Config:
    youtube_studio_url: str
    video_folder: Path
    uploaded_folder: Path
    failed_folder: Path
    logs_folder: Path
    default_visibility: str
    made_for_kids: bool
    stop_before_publish: bool
    chrome_user_data_dir: str
    channel_switch_enabled: bool
    channel_switch_match_texts: list[str]
    active_channel: str
    max_videos_per_run: int
    limit_scope: str
    schedule_enabled: bool
    manual_schedule_enabled: bool
    schedule_start_datetime: str
    schedule_interval_hours: int
    schedule_daily_times: list[str]
    schedule_language_slot: str
    schedule_slot_offset_minutes: int
    schedule_date_format: str
    schedule_time_format: str
    move_failed_to_failed: bool
    max_next_clicks: int


@dataclass(frozen=True)
class VideoPair:
    video_path: Path
    metadata_path: Path


@dataclass(frozen=True)
class Metadata:
    title: str
    description: str


class UploadUnavailableError(RuntimeError):
    """YouTube says this channel cannot upload more videos right now."""


LogCallback = Callable[[str], None]
ConfirmationCallback = Callable[[VideoPair, Metadata, Config, datetime | None], str]
UploadedCallback = Callable[[dict[str, str]], None]


class CallbackLogHandler(logging.Handler):
    def __init__(self, callback: LogCallback) -> None:
        super().__init__()
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.callback(self.format(record))
        except Exception:
            self.handleError(record)


class SafeConsoleHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            try:
                self.stream.write(message + self.terminator)
            except UnicodeEncodeError:
                buffer = getattr(self.stream, "buffer", None)
                if buffer is not None:
                    buffer.write((message + self.terminator).encode("utf-8", errors="replace"))
                else:
                    encoding = getattr(self.stream, "encoding", None) or "utf-8"
                    safe_message = message.encode(encoding, errors="replace").decode(
                        encoding,
                        errors="replace",
                    )
                    self.stream.write(safe_message + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)


def load_config(path: Path) -> Config:
    defaults = {
        "youtube_studio_url": "https://studio.youtube.com",
        "video_folder": "videos",
        "uploaded_folder": "uploaded",
        "failed_folder": "failed",
        "logs_folder": "logs",
        "default_visibility": "private",
        "made_for_kids": False,
        "stop_before_publish": True,
        "chrome_user_data_dir": "chrome-profile",
        "channel_switch_enabled": False,
        "channel_switch_match_texts": [],
        "active_channel": "default",
        "max_videos_per_run": 0,
        "limit_scope": "run",
        "channel_schedules": {},
        "schedule_enabled": False,
        "manual_schedule_enabled": False,
        "schedule_start_datetime": "",
        "schedule_interval_hours": 8,
        "schedule_daily_times": [],
        "schedule_language_slot": "manual",
        "schedule_slot_offset_minutes": 0,
        "schedule_date_format": "%m/%d/%Y",
        "schedule_time_format": "%H:%M",
        "move_failed_to_failed": True,
        "max_next_clicks": 5,
    }

    if not path.exists():
        path.write_text(json.dumps(defaults, indent=2, ensure_ascii=False), encoding="utf-8")

    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    data = {**defaults, **raw}

    visibility = str(data["default_visibility"]).strip().lower()
    if visibility not in VISIBILITY_TO_RADIO_NAME:
        raise ValueError("default_visibility must be one of: private, unlisted, public")

    active_channel = str(data.get("active_channel") or "default").strip() or "default"
    channel_schedule = get_channel_schedule(data, active_channel)
    schedule_start_datetime = str(
        channel_schedule.get("schedule_start_datetime", data["schedule_start_datetime"])
    ).strip()
    schedule_interval_hours = int(
        channel_schedule.get("schedule_interval_hours", data["schedule_interval_hours"])
    )
    schedule_daily_times = normalize_schedule_daily_times(
        channel_schedule.get(
            "schedule_daily_times",
            channel_schedule.get("daily_times", data.get("schedule_daily_times") or []),
        )
    )
    schedule_language_slot = str(
        channel_schedule.get("schedule_language_slot", data.get("schedule_language_slot", "manual"))
    ).strip().lower() or "manual"
    if schedule_language_slot not in SCHEDULE_SLOT_OFFSETS:
        schedule_language_slot = "manual"
    schedule_slot_offset_minutes = int(
        channel_schedule.get(
            "schedule_slot_offset_minutes",
            data.get(
                "schedule_slot_offset_minutes",
                SCHEDULE_SLOT_OFFSETS[schedule_language_slot],
            ),
        )
    )
    if schedule_language_slot != "manual":
        schedule_slot_offset_minutes = SCHEDULE_SLOT_OFFSETS[schedule_language_slot]

    if data["schedule_enabled"] and not schedule_start_datetime:
        raise ValueError("schedule_start_datetime is required when schedule_enabled=true")

    max_videos_per_run = int(
        channel_schedule.get("max_videos_per_run", data.get("max_videos_per_run") or 0)
    )
    limit_scope = str(data.get("limit_scope") or "run").strip().lower()
    if limit_scope in {"source_24h", "source_schedule_day"}:
        # Legacy configs may still carry historical quota modes. The current GUI
        # limits only the visible run batch and does not apply local 24h/day caps.
        limit_scope = "run"
    elif limit_scope != "run":
        raise ValueError("limit_scope must be 'run'")

    video_folder = resolve_project_path(data["video_folder"])
    validate_channel_folder_alignment(video_folder, active_channel)
    if bool(data.get("channel_switch_enabled", False)):
        raise ValueError(
            "Automatic channel switching is disabled. "
            "Open the needed YouTube channel manually before Start."
        )

    return Config(
        youtube_studio_url=str(data["youtube_studio_url"]).rstrip("/"),
        video_folder=video_folder,
        uploaded_folder=resolve_project_path(data["uploaded_folder"]),
        failed_folder=resolve_project_path(data["failed_folder"]),
        logs_folder=resolve_project_path(data["logs_folder"]),
        default_visibility=visibility,
        made_for_kids=bool(data["made_for_kids"]),
        stop_before_publish=bool(data["stop_before_publish"]),
        chrome_user_data_dir=str(data["chrome_user_data_dir"]).strip(),
        channel_switch_enabled=bool(data.get("channel_switch_enabled", False)),
        channel_switch_match_texts=[
            str(item).strip()
            for item in data.get("channel_switch_match_texts", [])
            if str(item).strip()
        ],
        active_channel=active_channel,
        max_videos_per_run=max(0, max_videos_per_run),
        limit_scope=limit_scope,
        schedule_enabled=bool(data["schedule_enabled"]),
        manual_schedule_enabled=bool(data.get("manual_schedule_enabled", False)),
        schedule_start_datetime=schedule_start_datetime,
        schedule_interval_hours=schedule_interval_hours,
        schedule_daily_times=schedule_daily_times,
        schedule_language_slot=schedule_language_slot,
        schedule_slot_offset_minutes=schedule_slot_offset_minutes,
        schedule_date_format=str(data["schedule_date_format"]),
        schedule_time_format=str(data["schedule_time_format"]),
        move_failed_to_failed=bool(data["move_failed_to_failed"]),
        max_next_clicks=int(data["max_next_clicks"]),
    )


def resolve_project_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return BASE_DIR / path


def get_channel_schedule(data: dict, active_channel: str) -> dict:
    schedules = data.get("channel_schedules") or {}
    if not isinstance(schedules, dict):
        raise ValueError("channel_schedules must be a JSON object")

    schedule = schedules.get(active_channel, {})
    if not schedule and active_channel != "default":
        schedule = schedules.get("default", {})

    if schedule and not isinstance(schedule, dict):
        raise ValueError(f"channel_schedules.{active_channel} must be a JSON object")

    return schedule


def normalize_schedule_daily_times(value: object) -> list[str]:
    if value in (None, ""):
        return []

    if isinstance(value, str):
        raw_times = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        raw_times = [str(part).strip() for part in value]
    else:
        raise ValueError("schedule_daily_times must be a list or comma-separated string")

    normalized = []
    for raw_time in raw_times:
        if not raw_time:
            continue
        parsed = parse_time_of_day(raw_time)
        normalized.append(f"{parsed.hour:02d}:{parsed.minute:02d}")

    return sorted(dict.fromkeys(normalized))


def parse_time_of_day(value: str) -> datetime:
    for time_format in ["%H:%M", "%H.%M", "%I:%M %p", "%I:%M%p"]:
        try:
            return datetime.strptime(value.strip(), time_format)
        except ValueError:
            continue
    raise ValueError(f"Invalid schedule time '{value}'. Use HH:MM, for example 10:00")


def ensure_directories(config: Config) -> None:
    for folder in [
        config.video_folder,
        config.uploaded_folder,
        config.failed_folder,
        config.logs_folder,
    ]:
        folder.mkdir(parents=True, exist_ok=True)


def setup_logging(logs_folder: Path, log_callback: LogCallback | None = None) -> logging.Logger:
    logs_folder.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("youtube_uploader")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(logs_folder / "uploader.log", encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_stream = sys.stdout or sys.stderr
    if console_stream is not None:
        console_handler = SafeConsoleHandler(console_stream)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    if log_callback is not None:
        callback_handler = CallbackLogHandler(log_callback)
        callback_handler.setFormatter(formatter)
        logger.addHandler(callback_handler)
    return logger


def find_video_pairs(video_folder: Path, logger: logging.Logger) -> list[VideoPair]:
    videos = sorted(
        path
        for path in video_folder.iterdir()
        if path.is_file() and path.suffix.lower() == ".mp4"
    )

    pairs: list[VideoPair] = []
    for video_path in videos:
        metadata_path = find_metadata_for_video(video_path, logger)
        if metadata_path is not None:
            pairs.append(VideoPair(video_path=video_path, metadata_path=metadata_path))
        else:
            logger.warning(
                "Skipping %s: matching metadata file not found (%s)",
                video_path.name,
                ", ".join(METADATA_SUFFIXES),
            )

    txt_stems = {
        metadata_video_stem(path)
        for path in video_folder.iterdir()
        if path.is_file() and path.suffix.lower() in METADATA_SUFFIXES
    }
    video_stems = {path.stem for path in videos}
    for orphan_stem in sorted(txt_stems - video_stems):
        logger.warning("Skipping %s metadata: matching .mp4 file not found", orphan_stem)

    return pairs


def normalized_path(path: Path) -> str:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    return os.path.normcase(str(resolved))


def parse_report_datetime(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    for date_format, length in [("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)]:
        try:
            return datetime.strptime(value[:length], date_format)
        except ValueError:
            continue
    return None


def limit_video_pairs(pairs: list[VideoPair], config: Config) -> list[VideoPair]:
    if config.max_videos_per_run <= 0:
        return pairs
    return pairs[: config.max_videos_per_run]


def find_metadata_for_video(video_path: Path, logger: logging.Logger) -> Path | None:
    candidates = [
        video_path.with_name(pattern.format(stem=video_path.stem))
        for pattern in METADATA_NAME_PATTERNS
    ]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return None

    return existing[0]


def metadata_video_stem(path: Path) -> str:
    stem = path.stem
    for suffix in ["_youtube", "_meta"]:
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def parse_metadata(path: Path) -> Metadata:
    if path.suffix.lower() == ".json":
        return parse_json_metadata(path)
    if path.suffix.lower() != ".txt":
        raise ValueError(f"Unsupported metadata file type: {path.suffix}")

    text = path.read_text(encoding="utf-8-sig")
    lines = [line.rstrip() for line in text.splitlines()]
    lines = trim_blank_edges(lines)

    if not lines:
        raise ValueError(f"{path.name} is empty")

    title, description = parse_marked_metadata(lines)
    if title:
        return Metadata(title=title, description=description)

    return parse_legacy_metadata(lines)


def parse_json_metadata(path: Path) -> Metadata:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))

    if isinstance(raw, list):
        if not raw:
            raise ValueError(f"{path.name} contains an empty JSON list")
        raw = raw[0]

    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} must contain a JSON object")

    title = clean_title(
        clean_text(
        first_json_value(
            raw,
            [
                "title",
                "TITLE",
                "name",
                "headline",
                "video_title",
                "videoTitle",
                "youtube_title",
                "youtubeTitle",
                "heading",
                "Заголовок",
                "заголовок",
                "Название",
                "название",
                "titre",
                "título",
                "titulo",
                "titel",
            ],
        )
        )
    )
    if not title:
        raise ValueError(f"{path.name}: JSON title is empty")

    description = clean_text(
        first_json_value(
            raw,
            [
                "description",
                "DESCRIPTION",
                "desc",
                "caption",
                "body",
                "text",
                "video_description",
                "videoDescription",
                "youtube_description",
                "youtubeDescription",
                "Beschreibung",
                "beschreibung",
                "Описание",
                "описание",
                "descripción",
                "descripcion",
                "descrição",
                "descricao",
            ],
        )
    )

    hashtags = clean_hashtags(
        first_json_value(
            raw,
            [
                "hashtags",
                "hashTags",
                "tags",
                "keywords",
                "Hashtags",
                "Tags",
                "Ключевые слова",
                "ключевые слова",
            ],
        )
    )

    if hashtags:
        if description:
            description = f"{description}\n\n{hashtags}"
        else:
            description = hashtags

    return Metadata(title=title, description=description)


def first_json_value(data: dict, keys: list[str]) -> object:
    nested_sections = [
        "metadata",
        "meta",
        "youtube",
        "youtube_metadata",
        "youtubeMetadata",
        "video",
        "short",
    ]

    exact_value = first_json_value_shallow(data, keys)
    if exact_value not in (None, ""):
        return exact_value

    lowered = {str(key).lower(): value for key, value in data.items()}
    for key in keys:
        value = lowered.get(key.lower(), "")
        if value not in (None, ""):
            return value

    for section in nested_sections:
        nested = data.get(section) or lowered.get(section.lower())
        if isinstance(nested, dict):
            nested_value = first_json_value(nested, keys)
            if nested_value not in (None, ""):
                return nested_value

    return ""


def first_json_value_shallow(data: dict, keys: list[str]) -> object:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return ""


def clean_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item).strip()).strip()
    if isinstance(value, dict):
        return "\n".join(
            str(item).strip()
            for item in value.values()
            if item is not None and str(item).strip()
        ).strip()
    return str(value).strip()


def clean_title(value: str) -> str:
    return re.sub(
        r"^(?:en|fr|es|de|ru|pt|ja|jp|hi|ar|zh|cn)[-_]\s*",
        "",
        value.strip(),
        flags=re.IGNORECASE,
    )


def clean_hashtags(value: object) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        parts = value.replace(",", " ").split()
    elif isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
    else:
        parts = [str(value).strip()]

    tags = []
    for part in parts:
        tag = part.strip()
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = f"#{tag}"
        tags.append(tag)
    return " ".join(tags)


def parse_marked_metadata(lines: list[str]) -> tuple[str, str]:
    title_marker_index = find_marker(lines, "TITLE")
    description_marker_index = find_marker(lines, "DESCRIPTION")
    if title_marker_index is None:
        return "", ""

    title_search_end = description_marker_index if description_marker_index is not None else len(lines)
    title = first_non_empty(lines[title_marker_index + 1 : title_search_end])
    if not title:
        raise ValueError("TITLE marker found, but title line is empty")

    if description_marker_index is None:
        description_lines = lines[title_marker_index + 1 :]
        description_lines = description_lines[1:] if description_lines else []
    else:
        description_lines = lines[description_marker_index + 1 :]

    description = "\n".join(trim_blank_edges(description_lines)).strip()
    return clean_title(title), description


def parse_legacy_metadata(lines: list[str]) -> Metadata:
    title_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if title_index is None:
        raise ValueError("metadata title is empty")

    title = clean_title(lines[title_index].strip())
    description_lines = trim_blank_edges(lines[title_index + 1 :])
    description = "\n".join(description_lines).strip()
    return Metadata(title=title, description=description)


def find_marker(lines: list[str], marker: str) -> int | None:
    marker_upper = marker.upper()
    for index, line in enumerate(lines):
        if line.strip().upper() == marker_upper:
            return index
    return None


def first_non_empty(lines: Iterable[str]) -> str:
    return next((line.strip() for line in lines if line.strip()), "")


def trim_blank_edges(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def parse_schedule_start(value: str) -> datetime:
    formats = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for date_format in formats:
        try:
            return datetime.strptime(value, date_format)
        except ValueError:
            continue
    raise ValueError(
        "schedule_start_datetime must look like '2026-05-14 18:30' "
        "or '2026-05-14T18:30'"
    )


def schedule_time_for_index(
    config: Config,
    index: int,
    schedule_start: datetime | None,
) -> datetime | None:
    if not config.schedule_enabled or schedule_start is None:
        return None

    if config.schedule_daily_times:
        return schedule_time_from_daily_slots(
            schedule_start,
            config.schedule_daily_times,
            index,
            config.schedule_slot_offset_minutes,
        )

    return schedule_start + timedelta(hours=config.schedule_interval_hours * index)


def schedule_time_from_daily_slots(
    schedule_start: datetime,
    daily_times: list[str],
    index: int,
    offset_minutes: int = 0,
) -> datetime:
    remaining = index
    day = schedule_start.date()
    group_index = 0
    parsed_times = [
        parse_time_of_day(daily_time).time().replace(second=0, microsecond=0)
        for daily_time in daily_times
    ]

    while True:
        group_offset_minutes = offset_minutes + (group_index * SCHEDULE_GROUP_STEP_MINUTES)
        candidates = []
        for parsed_time in parsed_times:
            candidate = datetime.combine(day, parsed_time)
            candidate += timedelta(minutes=group_offset_minutes)
            candidates.append(candidate)

        for candidate in sorted(candidates):
            if candidate < schedule_start:
                continue
            if remaining == 0:
                return candidate
            remaining -= 1
        day += timedelta(days=1)
        group_index += 1


def human_pause(min_seconds: float = 0.7, max_seconds: float = 1.8) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


def text_regex(labels: Iterable[str]) -> re.Pattern[str]:
    escaped = [re.escape(label) for label in labels]
    return re.compile("|".join(escaped), re.IGNORECASE)


def exact_text_regex(label: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*{re.escape(label)}\s*$", re.IGNORECASE)


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def retry_action(
    action: Callable[[], None],
    description: str,
    logger: logging.Logger,
    attempts: int = 3,
    delay_seconds: float = 1.5,
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            action()
            return
        except Exception as error:
            last_error = error
            if attempt == attempts:
                break
            logger.info(
                "%s failed on attempt %s/%s; retrying",
                description,
                attempt,
                attempts,
            )
            time.sleep(delay_seconds * attempt)
    raise RuntimeError(f"{description} failed after {attempts} attempts") from last_error


def visible_locator(candidates: Iterable[Locator], timeout_ms: int = 1_500) -> Locator | None:
    for locator in candidates:
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except PlaywrightTimeoutError:
            continue
    return None


def upload_unavailable_message(page: Page) -> str | None:
    try:
        result = page.evaluate(
            """
            (markersArg) => {
                const normalize = (value) => (value || "")
                    .normalize("NFD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .toLowerCase()
                    .replace(/\\s+/g, " ")
                    .trim();
                const markers = markersArg.map(normalize);
                const allElements = (root = document) => {
                    const result = [];
                    const visit = (node) => {
                        if (!node) {
                            return;
                        }
                        if (node.nodeType === Node.ELEMENT_NODE) {
                            result.push(node);
                            if (node.shadowRoot) {
                                visit(node.shadowRoot);
                            }
                        }
                        for (const child of Array.from(node.children || [])) {
                            visit(child);
                        }
                    };
                    visit(root.documentElement || root);
                    return result;
                };
                const visible = (element) => {
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 0
                        && rect.height > 0
                        && style.display !== "none"
                        && style.visibility !== "hidden"
                        && style.opacity !== "0";
                };
                const textOf = (element) => normalize([
                    element.getAttribute?.("aria-label"),
                    element.getAttribute?.("title"),
                    element.innerText,
                    element.textContent,
                    element.shadowRoot ? element.shadowRoot.textContent : "",
                ].filter(Boolean).join(" "));
                for (const element of allElements()) {
                    if (!visible(element)) {
                        continue;
                    }
                    const text = textOf(element);
                    if (!text) {
                        continue;
                    }
                    if (markers.some((marker) => marker && text.includes(marker))) {
                        return text.slice(0, 500);
                    }
                }
                return null;
            }
            """,
            UPLOAD_UNAVAILABLE_MARKERS,
        )
    except Exception:
        return None
    return str(result) if result else None


def ensure_upload_is_available(page: Page, logger: logging.Logger) -> None:
    deadline = time.monotonic() + 25
    title_seen_at: float | None = None

    while time.monotonic() < deadline:
        message = upload_unavailable_message(page)
        if message:
            clean_message = " ".join(message.split())
            if len(clean_message) > 280:
                clean_message = clean_message[:280].rstrip() + "..."
            raise UploadUnavailableError(
                "YouTube says uploading is unavailable for this channel right now. "
                "The current file was left in the source folder. "
                f"Message: {clean_message}"
            )

        title_box = visible_locator(title_textbox_candidates(page), timeout_ms=500)
        if title_box is not None:
            if title_seen_at is None:
                title_seen_at = time.monotonic()
            elif time.monotonic() - title_seen_at >= 5:
                return

        time.sleep(0.5)

    logger.warning("Upload form readiness check timed out; continuing with normal field filling")


def click_locator(locator: Locator, description: str) -> None:
    locator.scroll_into_view_if_needed(timeout=5_000)
    locator.click(timeout=10_000)
    human_pause()


def click_first_available(
    candidates: Iterable[Locator],
    description: str,
    logger: logging.Logger,
    attempts: int = 3,
) -> None:
    def action() -> None:
        locator = visible_locator(candidates, timeout_ms=3_000)
        if locator is None:
            raise PlaywrightTimeoutError(f"{description} not visible")
        click_locator(locator, description)

    retry_action(action, description, logger, attempts=attempts)


def dismiss_video_checks_warning_dialog(page: Page, logger: logging.Logger) -> bool:
    try:
        result = page.evaluate(
            """
            () => {
                const allElements = (root = document) => {
                    const result = [];
                    const visit = (node) => {
                        if (!node) {
                            return;
                        }
                        if (node.nodeType === Node.ELEMENT_NODE) {
                            result.push(node);
                            if (node.shadowRoot) {
                                visit(node.shadowRoot);
                            }
                        }
                        for (const child of Array.from(node.children || [])) {
                            visit(child);
                        }
                    };
                    visit(root.documentElement || root);
                    return result;
                };
                const normalize = (value) => (value || "")
                    .normalize("NFD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .toLowerCase()
                    .replace(/\\s+/g, " ")
                    .trim();
                const visible = (element) => {
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 0
                        && rect.height > 0
                        && style.display !== "none"
                        && style.visibility !== "hidden";
                };
                const textOf = (element) => normalize([
                    element.getAttribute?.("aria-label"),
                    element.getAttribute?.("title"),
                    element.innerText,
                    element.textContent,
                    element.shadowRoot ? element.shadowRoot.textContent : ""
                ].filter(Boolean).join(" "));
                const clickElement = (element) => {
                    const rect = element.getBoundingClientRect();
                    const centerX = rect.left + rect.width / 2;
                    const centerY = rect.top + rect.height / 2;
                    const target = document.elementFromPoint(centerX, centerY) || element;
                    if (typeof target.click === "function") {
                        target.click();
                    }
                    for (const eventName of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
                        target.dispatchEvent(new MouseEvent(eventName, {
                            bubbles: true,
                            cancelable: true,
                            view: window,
                            clientX: centerX,
                            clientY: centerY,
                        }));
                    }
                };
                const warningMarkers = [
                    "video checks still running",
                    "checks are still running",
                    "checking video still in progress",
                    "проверка видео еще продолжается",
                    "проверка видео ещё продолжается",
                    "проверка еще продолжается",
                    "проверка ещё продолжается",
                    "la verification de la video est toujours en cours",
                    "la vérification de la vidéo est toujours en cours",
                    "la comprobacion del video aun esta en curso",
                    "la comprobación del video aún está en curso",
                    "die videoprufung lauft noch",
                    "die videoprüfung läuft noch"
                ].map(normalize);
                const buttonLabels = new Set(["ok", "ок", "oк"]);
                const elements = allElements();
                const dialogs = elements.filter((element) => {
                    const tag = element.tagName.toLowerCase();
                    return visible(element)
                        && (
                            element.getAttribute("role") === "dialog"
                            || tag.includes("dialog")
                            || tag === "tp-yt-paper-dialog"
                        );
                });
                for (const dialog of dialogs) {
                    const text = textOf(dialog);
                    if (!warningMarkers.some((marker) => text.includes(marker))) {
                        continue;
                    }
                    const dialogRect = dialog.getBoundingClientRect();
                    const buttons = elements.filter((candidate) => {
                        if (!visible(candidate)) {
                            return false;
                        }
                        const rect = candidate.getBoundingClientRect();
                        const tag = candidate.tagName.toLowerCase();
                        const looksLikeButton = candidate.getAttribute("role") === "button"
                            || tag === "button"
                            || tag.includes("button");
                        return looksLikeButton
                            && rect.left >= dialogRect.left - 2
                            && rect.right <= dialogRect.right + 2
                            && rect.top >= dialogRect.top - 2
                            && rect.bottom <= dialogRect.bottom + 2;
                    });
                    const button = buttons.find((candidate) => {
                        const label = textOf(candidate);
                        return buttonLabels.has(label);
                    }) || buttons[buttons.length - 1];
                    if (!button) {
                        return { ok: false, reason: "checks warning button not found" };
                    }
                    clickElement(button);
                    return { ok: true, text: text.slice(0, 120) };
                }
                return { ok: false, reason: "checks warning dialog not found" };
            }
            """
        )
        if isinstance(result, dict) and result.get("ok"):
            logger.info("Dismissed YouTube checks warning dialog")
            human_pause(0.8, 1.5)
            return True
    except Exception as error:
        logger.debug("Could not dismiss checks warning dialog: %s", error)
    return False


def button_candidates(page: Page, labels: list[str]) -> list[Locator]:
    regex = text_regex(labels)
    return [
        page.get_by_role("button", name=regex).first,
        page.locator("ytcp-button").filter(has_text=regex).first,
        page.locator("tp-yt-paper-button").filter(has_text=regex).first,
        page.locator("button").filter(has_text=regex).first,
        page.get_by_text(regex).first,
    ]


def menu_item_candidates(page: Page, labels: list[str]) -> list[Locator]:
    regex = text_regex(labels)
    return [
        page.get_by_role("menuitem", name=regex).first,
        page.locator('[role="menuitem"]').filter(has_text=regex).first,
        page.locator("tp-yt-paper-item").filter(has_text=regex).first,
        page.locator("ytcp-ve").filter(has_text=regex).first,
        page.locator("yt-formatted-string").filter(has_text=regex).first,
        page.get_by_text(regex).first,
    ]


def open_create_menu(page: Page, logger: logging.Logger) -> None:
    dismiss_video_checks_warning_dialog(page, logger)
    if visible_share_dialog_is_open(page):
        logger.info("Lingering YouTube share dialog detected before opening Create menu")
        close_visible_share_dialog(page, logger)
    if final_success_dialog_is_visible(page):
        logger.info("Lingering YouTube success dialog detected before opening Create menu")
        close_final_success_dialog(page, logger)
    dismiss_overlay_backdrop(page, logger)
    candidates = button_candidates(page, UI_TEXT["create"])
    click_first_available(candidates, "Create menu", logger)


def click_upload_videos(page: Page, logger: logging.Logger) -> None:
    candidates = [
        *menu_item_candidates(page, UI_TEXT["upload_videos"]),
        *button_candidates(page, UI_TEXT["upload_videos"]),
    ]
    click_first_available(candidates, "Upload videos button", logger)


CHANNEL_SWITCH_TEXT = [
    "Switch account",
    "Change account",
    "Сменить аккаунт",
    "Переключить аккаунт",
    "Zmeniť účet",
    "Prepnúť účet",
    "Changer de compte",
    "Cambiar de cuenta",
    "Trocar de conta",
    "Mudar de conta",
    "Konto wechseln",
]

ACCESS_DENIED_MARKERS = [
    "you do not have access to this page",
    "you don't have access to this page",
    "no access to this page",
    "у вас нет доступа к этой странице",
    "войдите в аккаунт, которому предоставлено разрешение",
]


def account_menu_candidates(page: Page) -> list[Locator]:
    return [
        page.locator("#avatar-btn").first,
        page.locator("button#avatar-btn").first,
        page.locator('[aria-label*="Account" i]').first,
        page.locator('[aria-label*="Google Account" i]').first,
        page.locator('[aria-label*="Аккаунт" i]').first,
        page.locator("ytcp-topbar-menu-button-renderer").last,
        page.locator("ytd-topbar-menu-button-renderer").last,
    ]


def dismiss_overlay_backdrop(page: Page, logger: logging.Logger) -> None:
    try:
        result = page.evaluate(
            """
            () => {
                const visible = (element) => {
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 0
                        && rect.height > 0
                        && style.display !== "none"
                        && style.visibility !== "hidden"
                        && Number(style.opacity || "1") > 0;
                };
                const clickElement = (element) => {
                    const rect = element.getBoundingClientRect();
                    const centerX = rect.left + rect.width / 2;
                    const centerY = rect.top + rect.height / 2;
                    for (const eventName of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
                        element.dispatchEvent(new MouseEvent(eventName, {
                            bubbles: true,
                            cancelable: true,
                            view: window,
                            clientX: centerX,
                            clientY: centerY,
                        }));
                    }
                };
                const backdrops = Array.from(document.querySelectorAll("tp-yt-iron-overlay-backdrop"))
                    .filter((element) => element.hasAttribute("opened") && visible(element));
                if (!backdrops.length) {
                    return { closed: 0 };
                }
                for (const backdrop of backdrops) {
                    clickElement(backdrop);
                    backdrop.removeAttribute("opened");
                    backdrop.style.pointerEvents = "none";
                    backdrop.style.opacity = "0";
                    backdrop.style.display = "none";
                    if (typeof backdrop.remove === "function") {
                        backdrop.remove();
                    }
                }
                return { closed: backdrops.length };
            }
            """
        )
        if isinstance(result, dict) and int(result.get("closed") or 0) > 0:
            logger.info("Dismissed YouTube overlay backdrop(s): %s", result.get("closed"))
            human_pause(0.5, 1.0)
    except Exception as error:
        logger.debug("Could not dismiss YouTube overlay backdrop: %s", error)


def open_account_menu(page: Page, logger: logging.Logger) -> None:
    def action() -> None:
        dismiss_overlay_backdrop(page, logger)
        locator = visible_locator(account_menu_candidates(page), timeout_ms=3_000)
        if locator is None:
            raise PlaywrightTimeoutError("Account menu not visible")
        locator.scroll_into_view_if_needed(timeout=5_000)
        locator.click(timeout=10_000, force=True)
        human_pause()

    retry_action(action, "Account menu", logger, attempts=2)
    human_pause(0.8, 1.5)


def click_switch_account(page: Page, logger: logging.Logger) -> None:
    candidates = [
        *menu_item_candidates(page, CHANNEL_SWITCH_TEXT),
        *button_candidates(page, CHANNEL_SWITCH_TEXT),
        page.get_by_text(text_regex(CHANNEL_SWITCH_TEXT)).first,
    ]
    click_first_available(candidates, "Switch account menu item", logger, attempts=2)
    human_pause(1.0, 2.0)


def click_visible_channel_match(page: Page, match_texts: list[str], logger: logging.Logger) -> bool:
    result = page.evaluate(
        """
        (matchTexts) => {
            const normalize = (value) => (value || "")
                .normalize("NFD")
                .replace(/[\\u0300-\\u036f]/g, "")
                .toLowerCase()
                .replace(/\\s+/g, " ")
                .trim();
            const targets = matchTexts.map(normalize).filter(Boolean);
            const allElements = (root = document) => {
                const result = [];
                const visit = (node) => {
                    if (!node) {
                        return;
                    }
                    if (node.nodeType === Node.ELEMENT_NODE) {
                        result.push(node);
                        if (node.shadowRoot) {
                            visit(node.shadowRoot);
                        }
                    }
                    for (const child of Array.from(node.children || [])) {
                        visit(child);
                    }
                };
                visit(root);
                return result;
            };
            const visible = (element) => {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return rect.width > 0
                    && rect.height > 0
                    && style.visibility !== "hidden"
                    && style.display !== "none"
                    && Number(style.opacity || "1") > 0;
            };
            const textOf = (element) => normalize(
                element.innerText || element.textContent || element.getAttribute("aria-label") || ""
            );
            const clickElement = (element) => {
                const rect = element.getBoundingClientRect();
                const centerX = rect.left + rect.width / 2;
                const centerY = rect.top + rect.height / 2;
                element.scrollIntoView({ block: "center", inline: "center" });
                for (const eventName of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
                    element.dispatchEvent(new MouseEvent(eventName, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        clientX: centerX,
                        clientY: centerY,
                    }));
                }
            };
            const clickableAncestor = (element) => element.closest(
                "a, button, [role='button'], [role='menuitem'], [role='option'], "
                + "tp-yt-paper-item, ytd-account-item-renderer, ytd-compact-link-renderer"
            ) || element;

            const candidates = allElements()
                .filter(visible)
                .map((element) => ({ element, text: textOf(element) }))
                .filter((item) => item.text.length > 0 && item.text.length < 500)
                .filter((item) => targets.some((target) => item.text.includes(target)))
                .sort((left, right) => left.text.length - right.text.length);

            if (!candidates.length) {
                const visibleTexts = allElements()
                    .filter(visible)
                    .map(textOf)
                    .filter((text) => text.length > 0 && text.length < 160)
                    .slice(0, 80);
                return { ok: false, visibleTexts };
            }

            const target = clickableAncestor(candidates[0].element);
            clickElement(target);
            return { ok: true, text: candidates[0].text };
        }
        """,
        match_texts,
    )
    if isinstance(result, dict) and result.get("ok"):
        logger.info("Clicked channel switch target: %s", result.get("text", ""))
        human_pause(2.0, 4.0)
        return True

    visible_texts = []
    if isinstance(result, dict):
        visible_texts = result.get("visibleTexts") or []
    logger.info("Target channel was not visible in current menu. Visible menu text sample: %s", visible_texts)
    return False


def assert_studio_accessible(page: Page, logger: logging.Logger) -> None:
    try:
        body_text = strip_accents(page.locator("body").inner_text(timeout=5_000)).lower()
    except Exception:
        body_text = ""
    for marker in ACCESS_DENIED_MARKERS:
        if strip_accents(marker).lower() in body_text:
            raise RuntimeError("YouTube Studio says this Chrome profile has no access to the selected channel")

    create_button = visible_locator(button_candidates(page, UI_TEXT["create"]), timeout_ms=15_000)
    if create_button is None:
        raise RuntimeError("Cannot verify selected YouTube channel: Create button is not visible")
    logger.info("Selected YouTube channel is accessible")


def switch_youtube_channel_if_needed(page: Page, config: Config, logger: logging.Logger) -> None:
    if not config.channel_switch_enabled:
        return
    raise RuntimeError(
        "Automatic channel switching is disabled. "
        "Select the YouTube channel manually before starting the upload."
    )


def upload_file_input(page: Page) -> Locator:
    return page.locator('input[type="file"]').first


def upload_dialog_has_selected_file(page: Page, expected_name: str) -> bool:
    normalized_name = expected_name.strip().lower()
    if not normalized_name:
        return False

    title_box = visible_locator(title_textbox_candidates(page), timeout_ms=1_000)
    if title_box is not None:
        try:
            status = upload_dialog_status(page).lower()
        except Exception:
            status = ""
        if normalized_name in status:
            return True

    try:
        result = page.evaluate(
            """
            ({ expectedName }) => {
                const normalizedExpected = String(expectedName || "").trim().toLowerCase();
                if (!normalizedExpected) {
                    return false;
                }
                const dialog = document.querySelector("ytcp-uploads-dialog")
                    || document.querySelector("tp-yt-paper-dialog[opened]");
                const text = dialog ? (dialog.innerText || dialog.textContent || "") : "";
                return text.toLowerCase().includes(normalizedExpected);
            }
            """,
            {"expectedName": normalized_name},
        )
    except Exception:
        return False
    return bool(result)


def set_video_file(page: Page, video_path: Path, logger: logging.Logger) -> None:
    def action() -> None:
        file_input = upload_file_input(page)
        file_input.wait_for(state="attached", timeout=20_000)
        try:
            file_input.set_input_files(str(video_path.resolve()))
        except Exception:
            if not upload_dialog_has_selected_file(page, video_path.name):
                raise
            logger.warning(
                "Selecting video file timed out, but the upload dialog already shows %s; continuing",
                video_path.name,
            )
        human_pause(1.0, 2.4)

    retry_action(action, "Selecting video file", logger, attempts=2)


def title_textbox_candidates(page: Page) -> list[Locator]:
    title_regex = text_regex(
        [
            "Title",
            "Заголовок",
            "Название",
            "Názov",
            "Titre",
            "Título",
            "Titulo",
            "Titel",
        ]
    )
    return [
        page.locator("ytcp-video-title #textbox").first,
        page.locator("ytcp-social-suggestions-textbox").nth(0).locator("#textbox"),
        page.get_by_role("textbox", name=title_regex).first,
        page.locator('[contenteditable="true"]').nth(0),
    ]


def description_textbox_candidates(page: Page) -> list[Locator]:
    description_regex = text_regex(
        [
            "Description",
            "Описание",
            "Popis",
            "Opis",
            "Description",
            "Descripción",
            "Descricao",
            "Descrição",
            "Beschreibung",
        ]
    )
    return [
        page.locator("ytcp-video-description #textbox").first,
        page.locator("ytcp-social-suggestions-textbox").nth(1).locator("#textbox"),
        page.get_by_role("textbox", name=description_regex).first,
        page.locator('[contenteditable="true"]').nth(1),
    ]


def set_textbox_value_by_dom(locator: Locator, value: str) -> None:
    locator.evaluate(
        """
        (element, value) => {
            const setNativeValue = (node, nextValue) => {
                const prototype = Object.getPrototypeOf(node);
                const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
                if (descriptor && descriptor.set) {
                    descriptor.set.call(node, nextValue);
                } else {
                    node.value = nextValue;
                }
            };

            element.focus();
            if (element.isContentEditable || element.getAttribute("contenteditable") === "true") {
                element.textContent = value;
                element.dispatchEvent(new InputEvent("input", {
                    bubbles: true,
                    composed: true,
                    inputType: "insertText",
                    data: value,
                }));
                element.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
                return;
            }

            setNativeValue(element, value);
            element.dispatchEvent(new InputEvent("input", {
                bubbles: true,
                composed: true,
                inputType: "insertText",
                data: value,
            }));
            element.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
        }
        """,
        value,
    )


def fill_textbox(
    page: Page,
    candidates: list[Locator],
    value: str,
    description: str,
    logger: logging.Logger,
    attempts: int = 3,
    press_enter: bool = False,
) -> None:
    def action() -> None:
        locator = visible_locator(candidates, timeout_ms=5_000)
        if locator is None:
            raise PlaywrightTimeoutError(f"{description} textbox not visible")
        locator.scroll_into_view_if_needed(timeout=5_000)
        clicked = False
        try:
            locator.click(timeout=10_000)
            clicked = True
            locator.fill(value, timeout=10_000)
        except Exception as first_error:
            if not clicked:
                set_textbox_value_by_dom(locator, value)
            else:
                try:
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Backspace")
                    page.keyboard.insert_text(value)
                except Exception:
                    logger.info("Keyboard fallback failed for %s; using DOM fill", description)
                    set_textbox_value_by_dom(locator, value)
                else:
                    try:
                        text_now = locator.evaluate(
                            """element => element.value || element.innerText || element.textContent || """,
                        )
                    except Exception:
                        text_now = ""
                    if value.strip() not in str(text_now).strip():
                        logger.info("Keyboard fallback did not update %s; using DOM fill", description)
                        set_textbox_value_by_dom(locator, value)
            if first_error:
                human_pause(0.3, 0.8)
        if press_enter:
            page.keyboard.press("Enter")
        human_pause()

    retry_action(action, f"Filling {description}", logger, attempts=attempts)


def try_fill_textbox(
    page: Page,
    candidates: list[Locator],
    value: str,
    description: str,
    logger: logging.Logger,
    press_enter: bool = False,
) -> bool:
    try:
        fill_textbox(
            page,
            candidates,
            value,
            description,
            logger,
            attempts=1,
            press_enter=press_enter,
        )
        return True
    except Exception as error:
        logger.warning("Could not set %s automatically: %s", description, error)
        return False


def fill_title_and_description(
    page: Page,
    metadata: Metadata,
    logger: logging.Logger,
) -> None:
    fill_textbox(page, title_textbox_candidates(page), metadata.title, "title", logger)
    fill_textbox(
        page,
        description_textbox_candidates(page),
        metadata.description,
        "description",
        logger,
    )


def made_for_kids_no_candidates(page: Page) -> list[Locator]:
    regex = text_regex(UI_TEXT["made_for_kids_no"])
    return [
        page.locator('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]').first,
        page.locator('[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]').first,
        page.get_by_text(regex).first,
        page.locator("tp-yt-paper-radio-button").filter(has_text=regex).first,
    ]


def select_made_for_kids(page: Page, made_for_kids: bool, logger: logging.Logger) -> None:
    if made_for_kids:
        logger.warning(
            "made_for_kids=true is configured, but only the non-kids flow is implemented. "
            "Leaving the page for manual review."
        )
        return

    page.mouse.wheel(0, 900)
    human_pause(0.4, 1.0)
    click_first_available(
        made_for_kids_no_candidates(page),
        "No, it's not made for kids",
        logger,
    )


def next_button_candidates(page: Page) -> list[Locator]:
    return [
        page.locator("ytcp-button#next-button").first,
        page.locator("#next-button").first,
        *button_candidates(page, UI_TEXT["next"]),
    ]


def is_disabled(locator: Locator) -> bool:
    try:
        aria_disabled = locator.get_attribute("aria-disabled", timeout=500)
        disabled = locator.get_attribute("disabled", timeout=500)
        return aria_disabled == "true" or disabled is not None
    except Exception:
        return False


def click_next(page: Page, logger: logging.Logger) -> None:
    def action() -> None:
        locator = visible_locator(next_button_candidates(page), timeout_ms=5_000)
        if locator is None:
            raise PlaywrightTimeoutError("Next button not visible")

        for _ in range(20):
            if not is_disabled(locator):
                click_locator(locator, "Next button")
                return
            time.sleep(0.5)

        raise PlaywrightTimeoutError("Next button stayed disabled")

    retry_action(action, "Clicking Next", logger, attempts=3)


def visibility_radio_candidates(page: Page, visibility: str) -> list[Locator]:
    radio_name = VISIBILITY_TO_RADIO_NAME[visibility]
    text_key = f"visibility_{visibility}"
    regex = text_regex(UI_TEXT[text_key])
    return [
        page.locator(f'tp-yt-paper-radio-button[name="{radio_name}"]').first,
        page.locator(f'[name="{radio_name}"]').first,
        page.locator("tp-yt-paper-radio-button").filter(has_text=regex).first,
        page.get_by_text(regex).first,
    ]


def schedule_radio_candidates(page: Page) -> list[Locator]:
    regex = text_regex(UI_TEXT["schedule"])
    return [
        page.locator('tp-yt-paper-radio-button[name="SCHEDULE"]').first,
        page.locator('[name="SCHEDULE"]').first,
        page.locator("tp-yt-paper-radio-button").filter(has_text=regex).first,
        page.get_by_text(regex).first,
    ]


def visibility_screen_marker_candidates(page: Page) -> list[Locator]:
    candidates: list[Locator] = []
    for visibility in VISIBILITY_TO_RADIO_NAME:
        candidates.extend(visibility_radio_candidates(page, visibility)[:3])
    candidates.extend(schedule_radio_candidates(page)[:3])
    return candidates


def visibility_screen_is_visible(page: Page) -> bool:
    return visible_locator(visibility_screen_marker_candidates(page), timeout_ms=700) is not None


def advance_to_visibility_screen(page: Page, config: Config, logger: logging.Logger) -> None:
    for step in range(config.max_next_clicks + 1):
        dismiss_video_checks_warning_dialog(page, logger)
        if visibility_screen_is_visible(page):
            logger.info("Visibility screen detected")
            return
        if step == config.max_next_clicks:
            break
        logger.info("Advancing upload wizard: Next %s/%s", step + 1, config.max_next_clicks)
        click_next(page, logger)
        dismiss_video_checks_warning_dialog(page, logger)
        human_pause(1.0, 2.5)

    raise RuntimeError("Could not reach the visibility screen")


def select_visibility(page: Page, visibility: str, logger: logging.Logger) -> None:
    click_first_available(
        visibility_radio_candidates(page, visibility),
        f"Visibility option: {visibility}",
        logger,
    )


def schedule_date_input_candidates(page: Page) -> list[Locator]:
    return [
        page.locator("tp-yt-paper-dialog ytcp-date-picker input").first,
        page.locator("ytcp-date-picker input").first,
        page.get_by_role(
            "textbox",
            name=text_regex(["Date", "Дата", "Dátum", "Fecha", "Datum", "Data"]),
        ).first,
    ]


def schedule_date_trigger_candidates(page: Page) -> list[Locator]:
    return [
        page.locator("ytcp-date-picker ytcp-dropdown-trigger").first,
        page.locator("ytcp-date-picker #trigger").first,
        page.locator("ytcp-date-picker").first.locator('[role="button"]').first,
        page.locator("ytcp-datetime-picker ytcp-date-picker ytcp-dropdown-trigger").first,
        page.locator("ytcp-datetime-picker").first.locator("ytcp-dropdown-trigger").nth(0),
    ]


def schedule_day_button_candidates(page: Page, day: int) -> list[Locator]:
    regex = exact_text_regex(str(day))
    return [
        page.get_by_role("button", name=regex).first,
        page.locator("tp-yt-paper-dialog tp-yt-paper-button").filter(has_text=regex).first,
        page.locator('tp-yt-paper-dialog [role="button"]').filter(has_text=regex).first,
    ]


def read_locator_text(locator: Locator) -> str:
    try:
        value = locator.input_value(timeout=700).strip()
        if value:
            return value
    except Exception:
        pass
    try:
        return locator.inner_text(timeout=700).strip()
    except Exception:
        return ""


def month_name_lookup() -> dict[str, int]:
    names = {
        1: [
            "january",
            "jan",
            "январь",
            "января",
            "januar",
            "januara",
        ],
        2: [
            "february",
            "feb",
            "февраль",
            "февраля",
            "februar",
            "februara",
        ],
        3: [
            "march",
            "mar",
            "март",
            "марта",
            "marec",
            "marca",
        ],
        4: [
            "april",
            "apr",
            "апрель",
            "апреля",
            "april",
            "aprila",
        ],
        5: [
            "may",
            "май",
            "мая",
            "maj",
            "maja",
        ],
        6: [
            "june",
            "jun",
            "июнь",
            "июня",
            "jun",
            "juna",
        ],
        7: [
            "july",
            "jul",
            "июль",
            "июля",
            "jul",
            "jula",
        ],
        8: [
            "august",
            "aug",
            "август",
            "августа",
            "augusta",
        ],
        9: [
            "september",
            "sep",
            "сентябрь",
            "сентября",
            "septembra",
        ],
        10: [
            "october",
            "oct",
            "октябрь",
            "октября",
            "oktober",
            "oktobra",
        ],
        11: [
            "november",
            "nov",
            "ноябрь",
            "ноября",
            "novembra",
        ],
        12: [
            "december",
            "dec",
            "декабрь",
            "декабря",
            "decembra",
        ],
    }
    lookup: dict[str, int] = {}
    for month, aliases in names.items():
        for alias in aliases:
            lookup[strip_accents(alias.lower())] = month
    extra_aliases = {
        1: ["enero", "janvier", "janeiro", "\u044f\u043d\u0432"],
        2: ["febrero", "fevrier", "fevereiro", "\u0444\u0435\u0432"],
        3: ["marzo", "mars", "marco", "marz", "\u043c\u0430\u0440"],
        4: ["abril", "avril", "\u0430\u043f\u0440"],
        5: ["mayo", "mai", "\u043c\u0430\u0439"],
        6: ["junio", "juin", "junho", "juni", "\u0438\u044e\u043d"],
        7: ["julio", "juillet", "julho", "juli", "\u0438\u044e\u043b"],
        8: ["agosto", "aout", "\u0430\u0432\u0433"],
        9: ["septiembre", "septembre", "setembro", "\u0441\u0435\u043d", "\u0441\u0435\u043d\u0442"],
        10: ["octubre", "octobre", "outubro", "\u043e\u043a\u0442"],
        11: ["noviembre", "novembre", "novembro", "\u043d\u043e\u044f", "\u043d\u043e\u044f\u0431"],
        12: ["diciembre", "decembre", "dezembro", "dezember", "\u0434\u0435\u043a"],
    }
    for month, aliases in extra_aliases.items():
        for alias in aliases:
            lookup[strip_accents(alias.lower())] = month
    return lookup


def click_calendar_date_by_dom(
    page: Page,
    schedule_time: datetime,
    logger: logging.Logger,
) -> bool:
    aliases_by_month: dict[str, list[str]] = {str(month): [] for month in range(1, 13)}
    for alias, month in month_name_lookup().items():
        aliases_by_month[str(month)].append(alias)

    result = page.evaluate(
        """
        ({ day, month, year, monthAliases }) => {
            const allElements = (root = document) => {
                const result = [];
                const visit = (node) => {
                    if (!node) {
                        return;
                    }
                    if (node.nodeType === Node.ELEMENT_NODE) {
                        result.push(node);
                        if (node.shadowRoot) {
                            visit(node.shadowRoot);
                        }
                    }
                    for (const child of Array.from(node.children || [])) {
                        visit(child);
                    }
                };
                visit(root.documentElement || root);
                return result;
            };
            const normalize = (value) => (value || "")
                .normalize("NFD")
                .replace(/[\\u0300-\\u036f]/g, "")
                .toLowerCase()
                .replace(/\\s+/g, " ")
                .trim();
            const visible = (element) => {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return rect.width > 0
                    && rect.height > 0
                    && style.display !== "none"
                    && style.visibility !== "hidden";
            };
            const clickNode = (node) => {
                const clickable = node.closest(
                    "button,[role='button'],tp-yt-paper-button,ytcp-button"
                ) || node;
                if (!visible(clickable)) {
                    return null;
                }
                const rect = clickable.getBoundingClientRect();
                const centerX = rect.left + rect.width / 2;
                const centerY = rect.top + rect.height / 2;
                return {
                    clicked: true,
                    x: centerX,
                    y: centerY,
                    text: (clickable.getAttribute("aria-label")
                        || clickable.innerText
                        || clickable.textContent
                        || "").trim(),
                };
            };

            const nodes = allElements().filter(visible);
            const targetAliases = monthAliases[String(month)] || [];
            let currentMonth = null;
            let currentYear = null;
            const candidates = [];
            const fallbackDayCandidates = [];

            for (const node of nodes) {
                const rect = node.getBoundingClientRect();
                const area = rect.width * rect.height;
                const tag = node.tagName.toLowerCase();
                const disabled = node.disabled
                    || node.hasAttribute("disabled")
                    || node.getAttribute("aria-disabled") === "true";
                const attrText = normalize([
                    node.getAttribute("aria-label"),
                    node.getAttribute("title"),
                ].filter(Boolean).join(" "));
                const bodyText = normalize(node.innerText || node.textContent || "");
                const allText = `${attrText} ${bodyText}`.trim();
                if (!allText) {
                    continue;
                }

                const yearMatch = allText.match(/\\b(19\\d{2}|20\\d{2})\\b/);
                if (yearMatch) {
                    for (const [monthKey, aliases] of Object.entries(monthAliases)) {
                        if (aliases.some((alias) => allText.includes(normalize(alias)))) {
                            currentMonth = Number(monthKey);
                            currentYear = Number(yearMatch[1]);
                            break;
                        }
                    }
                }

                const hasTargetMonth = targetAliases.some((alias) => (
                    attrText.includes(normalize(alias))
                ));
                const hasTargetDay = new RegExp(`(^|\\\\D)${day}(\\\\D|$)`).test(attrText);
                if (
                    attrText
                    && hasTargetMonth
                    && attrText.includes(String(year))
                    && hasTargetDay
                    && area <= 5000
                ) {
                    candidates.push({ node, mode: "aria-label", area });
                }

                if (
                    bodyText === String(day)
                    && currentMonth === month
                    && currentYear === year
                    && area <= 5000
                    && !disabled
                ) {
                    candidates.push({ node, mode: "month-section", area });
                }
                const looksLikeDay = !disabled
                    && area <= 5000
                    && bodyText === String(day)
                    && (
                        node.getAttribute("role") === "button"
                        || tag === "button"
                        || tag.includes("button")
                        || node.closest?.("button,[role='button'],tp-yt-paper-button")
                    );
                if (looksLikeDay) {
                    fallbackDayCandidates.push({ node, mode: "unique-visible-day", area });
                }
            }

            candidates.sort((left, right) => left.area - right.area);
            for (const candidate of candidates) {
                const clicked = clickNode(candidate.node);
                if (clicked) {
                    return { ...clicked, mode: candidate.mode, area: candidate.area };
                }
            }
            if (fallbackDayCandidates.length === 1) {
                const candidate = fallbackDayCandidates[0];
                const clicked = clickNode(candidate.node);
                if (clicked) {
                    return { ...clicked, mode: candidate.mode, area: candidate.area };
                }
            }
            return { clicked: false, mode: "not-found" };
        }
        """,
        {
            "day": schedule_time.day,
            "month": schedule_time.month,
            "year": schedule_time.year,
            "monthAliases": aliases_by_month,
        },
    )
    if isinstance(result, dict) and result.get("clicked"):
        page.mouse.click(float(result["x"]), float(result["y"]))
        logger.info(
            "Selected schedule date in calendar using %s: %s",
            result.get("mode"),
            result.get("text", ""),
        )
        human_pause(1.0, 1.8)
        return True

    logger.warning("Could not find target date in the open calendar")
    return False


def parse_schedule_date_text(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None

    for date_format in ["%m/%d/%Y", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"]:
        match = re.search(r"\d{1,4}[./-]\d{1,2}[./-]\d{2,4}", text)
        if match:
            try:
                return datetime.strptime(match.group(0), date_format)
            except ValueError:
                continue

    normalized = strip_accents(text.lower())
    tokens = re.findall(r"[a-zа-яё]+|\d{1,4}", normalized)
    lookup = month_name_lookup()
    tokens = re.findall(r"[a-z\u0400-\u04ff]+|\d{1,4}", normalized)
    for index, token in enumerate(tokens):
        month = lookup.get(token)
        if month is None:
            continue

        year = None
        for candidate in tokens:
            if candidate.isdigit() and len(candidate) == 4:
                year = int(candidate)
                break
        if year is None:
            continue

        day = None
        for candidate in reversed(tokens[:index]):
            if candidate.isdigit() and 1 <= int(candidate) <= 31:
                day = int(candidate)
                break
        if day is None:
            for candidate in tokens[index + 1 :]:
                if candidate.isdigit() and len(candidate) != 4 and 1 <= int(candidate) <= 31:
                    day = int(candidate)
                    break
        if day is None:
            continue

        try:
            return datetime(year, month, day)
        except ValueError:
            return None

    return None


def selected_schedule_date(page: Page) -> datetime | None:
    candidates = [
        page.locator("ytcp-date-picker").first,
        page.locator("ytcp-datetime-picker ytcp-date-picker").first,
        *schedule_date_trigger_candidates(page),
    ]
    for locator in candidates:
        try:
            locator.wait_for(state="visible", timeout=500)
        except PlaywrightTimeoutError:
            continue
        parsed = parse_schedule_date_text(read_locator_text(locator))
        if parsed is not None:
            return parsed
    return None


def schedule_date_matches_target(page: Page, schedule_time: datetime) -> bool:
    selected = selected_schedule_date(page)
    return selected is not None and selected.date() == schedule_time.date()


def schedule_date_input_values(schedule_time: datetime, config: Config) -> list[str]:
    values = [
        schedule_time.strftime(config.schedule_date_format),
        schedule_time.strftime("%d.%m.%Y"),
        schedule_time.strftime("%d/%m/%Y"),
        schedule_time.strftime("%Y-%m-%d"),
    ]
    russian_months = {
        1: "янв",
        2: "фев",
        3: "мар",
        4: "апр",
        5: "мая",
        6: "июн",
        7: "июл",
        8: "авг",
        9: "сен",
        10: "окт",
        11: "ноя",
        12: "дек",
    }
    ru_month = russian_months[schedule_time.month]
    values.extend(
        [
            f"{schedule_time.day} {ru_month}. {schedule_time.year} г.",
            f"{schedule_time.day} {ru_month}. {schedule_time.year}\u202fг.",
        ]
    )

    unique_values = []
    for value in values:
        if value not in unique_values:
            unique_values.append(value)
    return unique_values


def set_schedule_date_by_keyboard(
    page: Page,
    schedule_time: datetime,
    logger: logging.Logger,
) -> bool:
    selected = selected_schedule_date(page)
    if selected is None:
        return False

    delta_days = (schedule_time.date() - selected.date()).days
    if delta_days == 0:
        return True
    if abs(delta_days) > 45:
        logger.debug("Skipping keyboard date selection for large delta: %s", delta_days)
        return False

    trigger = visible_locator(schedule_date_trigger_candidates(page), timeout_ms=2_000)
    if trigger is None:
        return False

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    click_locator(trigger, "schedule date selector")
    human_pause(0.4, 0.8)

    key = "ArrowRight" if delta_days > 0 else "ArrowLeft"
    for _ in range(abs(delta_days)):
        page.keyboard.press(key)
        human_pause(0.04, 0.1)
    page.keyboard.press("Enter")
    human_pause(0.8, 1.4)

    if schedule_date_matches_target(page, schedule_time):
        logger.info(
            "Selected schedule date by keyboard: %s -> %s",
            selected.strftime("%Y-%m-%d"),
            schedule_time.strftime("%Y-%m-%d"),
        )
        return True
    return False


def set_schedule_date_by_dom(page: Page, date_value: str, logger: logging.Logger) -> bool:
    result = page.evaluate(
        """
        ({ dateValue }) => {
            const allElements = (root = document) => {
                const result = [];
                const visit = (node) => {
                    if (!node) {
                        return;
                    }
                    if (node.nodeType === Node.ELEMENT_NODE) {
                        result.push(node);
                        if (node.shadowRoot) {
                            visit(node.shadowRoot);
                        }
                    }
                    for (const child of Array.from(node.children || [])) {
                        visit(child);
                    }
                };
                visit(root.documentElement || root);
                return result;
            };
            const visible = (element) => {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return rect.width > 0
                    && rect.height > 0
                    && style.display !== "none"
                    && style.visibility !== "hidden";
            };
            const labelOf = (input) => [
                input.getAttribute("aria-label"),
                input.getAttribute("placeholder"),
                input.getAttribute("name"),
                input.id,
                input.closest("ytcp-date-picker")?.innerText,
            ].filter(Boolean).join(" ");
            const inputs = allElements()
                .filter((element) => element.tagName?.toLowerCase() === "input")
                .filter(visible)
                .map((input) => ({
                    input,
                    value: input.value || "",
                    label: labelOf(input),
                    rect: input.getBoundingClientRect(),
                    inDatePicker: Boolean(input.closest("ytcp-date-picker")),
                }))
                .filter((item) => !/\\b([01]?\\d|2[0-3]):[0-5]\\d\\b/.test(item.value))
                .filter((item) => {
                    const haystack = `${item.value} ${item.label}`.toLowerCase();
                    const monthWords = /jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|янв|фев|мар|апр|мая|май|июн|июл|авг|сен|окт|ноя|дек|enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre|janvier|fevrier|février|mars|avril|mai|juin|juillet|aout|août|septembre|octobre|novembre|decembre|décembre|janeiro|fevereiro|marco|março|maio|junho|julho|setembro|outubro|dezembro/i;
                    if (item.inDatePicker || monthWords.test(haystack)) {
                        return true;
                    }
                    return /\\d{1,4}[./-]\\d{1,2}[./-]\\d{1,4}/.test(haystack)
                        || /date|datum|fecha|data|дата|публикац|publish|publication/i.test(haystack);
                });
            inputs.sort((left, right) => {
                if (left.inDatePicker !== right.inDatePicker) {
                    return left.inDatePicker ? -1 : 1;
                }
                const leftHint = /date|datum|fecha|data|дата|публикац|publish|publication/i.test(left.label) ? 0 : 1;
                const rightHint = /date|datum|fecha|data|дата|публикац|publish|publication/i.test(right.label) ? 0 : 1;
                if (leftHint !== rightHint) {
                    return leftHint - rightHint;
                }
                return left.rect.top - right.rect.top;
            });
            const item = inputs[0];
            if (!item) {
                return { ok: false, reason: "visible date input not found" };
            }
            const input = item.input;
            input.focus();
            input.select();
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype,
                "value"
            )?.set;
            if (setter) {
                setter.call(input, dateValue);
            } else {
                input.value = dateValue;
            }
            input.dispatchEvent(new InputEvent("input", {
                bubbles: true,
                cancelable: true,
                inputType: "insertText",
                data: dateValue,
            }));
            input.dispatchEvent(new Event("change", { bubbles: true }));
            for (const eventName of ["keydown", "keyup"]) {
                input.dispatchEvent(new KeyboardEvent(eventName, {
                    bubbles: true,
                    cancelable: true,
                    key: "Enter",
                    code: "Enter",
                }));
            }
            input.blur();
            return { ok: true, oldValue: item.value, newValue: input.value, label: item.label };
        }
        """,
        {"dateValue": date_value},
    )
    if isinstance(result, dict) and result.get("ok"):
        logger.info(
            "Set schedule date by direct input: %s -> %s",
            result.get("oldValue", ""),
            date_value,
        )
        human_pause(0.8, 1.5)
        return True
    logger.debug("Could not set schedule date by direct input: %s", result)
    return False


def schedule_time_input_candidates(page: Page) -> list[Locator]:
    return [
        page.locator("ytcp-datetime-picker input").first,
        page.locator("ytcp-time-of-day-picker input").first,
        page.get_by_role(
            "combobox",
            name=text_regex(["Time", "Время", "Čas", "Hora", "Heure", "Uhrzeit"]),
        ).first,
        page.get_by_role(
            "textbox",
            name=text_regex(["Time", "Время", "Čas", "Hora", "Heure", "Uhrzeit"]),
        ).first,
    ]


def parse_schedule_time_text(value: str) -> str | None:
    text = value.strip()
    match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if match:
        return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"

    match = re.search(r"\b(1[0-2]|0?\d):([0-5]\d)\s*(AM|PM)\b", text, re.IGNORECASE)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))
    period = match.group(3).upper()
    if period == "PM" and hour != 12:
        hour += 12
    if period == "AM" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def selected_schedule_time(page: Page) -> str | None:
    result = page.evaluate(
        """
        () => {
            const visible = (element) => {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return rect.width > 0
                    && rect.height > 0
                    && style.display !== "none"
                    && style.visibility !== "hidden";
            };
            const uploadDialog = document.querySelector("ytcp-uploads-dialog")
                || document.querySelector("tp-yt-paper-dialog[opened]")
                || document.body;
            const inputs = Array.from(uploadDialog.querySelectorAll("input"))
                .filter(visible)
                .map((input) => ({
                    value: input.value || "",
                    label: [
                        input.getAttribute("aria-label"),
                        input.getAttribute("placeholder"),
                        input.getAttribute("name"),
                        input.id,
                    ].filter(Boolean).join(" "),
                    rect: input.getBoundingClientRect(),
                }))
                .filter((item) => /\\b([01]?\\d|2[0-3]):[0-5]\\d\\b/.test(item.value))
                .filter((item) => !/[./-]\\d{1,2}[./-]/.test(item.value));

            inputs.sort((left, right) => {
                const leftTimeHint = /time|время|čas|hora|heure|uhrzeit/i.test(left.label) ? 0 : 1;
                const rightTimeHint = /time|время|čas|hora|heure|uhrzeit/i.test(right.label) ? 0 : 1;
                if (leftTimeHint !== rightTimeHint) {
                    return leftTimeHint - rightTimeHint;
                }
                return left.rect.top - right.rect.top;
            });
            return inputs[0]?.value || null;
        }
        """
    )
    if isinstance(result, str):
        return parse_schedule_time_text(result)
    return None


def schedule_time_matches_target(page: Page, schedule_time: datetime, config: Config) -> bool:
    return selected_schedule_time(page) == schedule_time.strftime(config.schedule_time_format)


def set_schedule_time_by_dom(page: Page, time_value: str, logger: logging.Logger) -> bool:
    result = page.evaluate(
        """
        ({ timeValue }) => {
            const visible = (element) => {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return rect.width > 0
                    && rect.height > 0
                    && style.display !== "none"
                    && style.visibility !== "hidden";
            };
            const uploadDialog = document.querySelector("ytcp-uploads-dialog")
                || document.querySelector("tp-yt-paper-dialog[opened]")
                || document.body;
            const inputs = Array.from(uploadDialog.querySelectorAll("input"))
                .filter(visible)
                .map((input) => ({
                    input,
                    value: input.value || "",
                    label: [
                        input.getAttribute("aria-label"),
                        input.getAttribute("placeholder"),
                        input.getAttribute("name"),
                        input.id,
                    ].filter(Boolean).join(" "),
                    rect: input.getBoundingClientRect(),
                }))
                .filter((item) => /\\b([01]?\\d|2[0-3]):[0-5]\\d\\b/.test(item.value))
                .filter((item) => !/[./-]\\d{1,2}[./-]/.test(item.value));

            inputs.sort((left, right) => {
                const leftTimeHint = /time|время|čas|hora|heure|uhrzeit/i.test(left.label) ? 0 : 1;
                const rightTimeHint = /time|время|čas|hora|heure|uhrzeit/i.test(right.label) ? 0 : 1;
                if (leftTimeHint !== rightTimeHint) {
                    return leftTimeHint - rightTimeHint;
                }
                return left.rect.top - right.rect.top;
            });

            const item = inputs[0];
            if (!item) {
                return { ok: false, reason: "visible time input not found" };
            }

            const input = item.input;
            input.focus();
            input.select();
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype,
                "value"
            )?.set;
            if (setter) {
                setter.call(input, timeValue);
            } else {
                input.value = timeValue;
            }
            input.dispatchEvent(new InputEvent("input", {
                bubbles: true,
                cancelable: true,
                inputType: "insertText",
                data: timeValue,
            }));
            input.dispatchEvent(new Event("change", { bubbles: true }));
            input.dispatchEvent(new KeyboardEvent("keydown", {
                bubbles: true,
                cancelable: true,
                key: "Enter",
                code: "Enter",
            }));
            input.dispatchEvent(new KeyboardEvent("keyup", {
                bubbles: true,
                cancelable: true,
                key: "Enter",
                code: "Enter",
            }));
            input.blur();
            return { ok: true, previous: item.value, current: input.value };
        }
        """,
        {"timeValue": time_value},
    )
    if isinstance(result, dict) and result.get("ok"):
        logger.info(
            "Set schedule time by direct input: %s -> %s",
            result.get("previous"),
            result.get("current"),
        )
        human_pause(0.5, 1.0)
        return True

    logger.warning("Could not set schedule time by direct input: %s", result)
    return False


def schedule_time_trigger_candidates(page: Page) -> list[Locator]:
    return [
        page.locator("ytcp-time-of-day-picker ytcp-dropdown-trigger").first,
        page.locator("ytcp-time-of-day-picker input").first,
        page.locator("ytcp-datetime-picker input").first,
        page.locator("ytcp-datetime-picker ytcp-time-of-day-picker ytcp-dropdown-trigger").first,
    ]


def schedule_time_option_candidates(page: Page, time_value: str) -> list[Locator]:
    regex = exact_text_regex(time_value)
    return [
        page.get_by_role("option", name=regex).first,
        page.locator("tp-yt-paper-item").filter(has_text=regex).first,
        page.locator('[role="option"]').filter(has_text=regex).first,
        page.get_by_text(regex).first,
    ]


def set_schedule_date(
    page: Page,
    schedule_time: datetime,
    config: Config,
    logger: logging.Logger,
) -> bool:
    if schedule_date_matches_target(page, schedule_time):
        logger.info("Schedule date already matches target: %s", schedule_time.strftime("%Y-%m-%d"))
        return True

    date_values = schedule_date_input_values(schedule_time, config)
    for date_value in date_values:
        if set_schedule_date_by_dom(page, date_value, logger) and schedule_date_matches_target(
            page,
            schedule_time,
        ):
            return True

    trigger = visible_locator(schedule_date_trigger_candidates(page), timeout_ms=2_000)
    if trigger is not None:
        for attempt in range(1, 4):
            if attempt == 1:
                click_locator(trigger, "schedule date selector")
            for date_value in date_values:
                if set_schedule_date_by_dom(page, date_value, logger) and schedule_date_matches_target(
                    page,
                    schedule_time,
                ):
                    return True
            if click_calendar_date_by_dom(page, schedule_time, logger) and schedule_date_matches_target(
                page,
                schedule_time,
            ):
                return True
            if set_schedule_date_by_keyboard(page, schedule_time, logger):
                return True
            logger.warning(
                "Schedule date attempt %s/3 did not stick; target=%s selected=%s",
                attempt,
                schedule_time.strftime("%Y-%m-%d"),
                selected_schedule_date(page).strftime("%Y-%m-%d")
                if selected_schedule_date(page)
                else "unknown",
            )
            human_pause(0.8, 1.5)
        selected = selected_schedule_date(page)
        logger.warning(
            "Schedule date mismatch after calendar click: target=%s selected=%s",
            schedule_time.strftime("%Y-%m-%d"),
            selected.strftime("%Y-%m-%d") if selected else "unknown",
        )
    return False


def set_schedule_time(page: Page, schedule_time: datetime, config: Config, logger: logging.Logger) -> bool:
    time_value = schedule_time.strftime(config.schedule_time_format)
    if schedule_time_matches_target(page, schedule_time, config):
        logger.info("Schedule time already matches target: %s", time_value)
        return True

    if set_schedule_time_by_dom(page, time_value, logger) and schedule_time_matches_target(
        page,
        schedule_time,
        config,
    ):
        return True

    trigger = visible_locator(schedule_time_trigger_candidates(page), timeout_ms=2_000)
    if trigger is not None:
        click_locator(trigger, "schedule time selector")
        try:
            click_first_available(
                schedule_time_option_candidates(page, time_value),
                "schedule time option",
                logger,
                attempts=2,
            )
            human_pause(0.4, 1.0)
            if schedule_time_matches_target(page, schedule_time, config):
                return True
        except Exception as error:
            logger.warning("Could not choose schedule time from the dropdown: %s", error)

    filled = try_fill_textbox(
        page,
        schedule_time_input_candidates(page),
        time_value,
        "schedule time",
        logger,
        press_enter=True,
    )
    return filled and schedule_time_matches_target(page, schedule_time, config)


def apply_schedule(
    page: Page,
    schedule_time: datetime,
    config: Config,
    logger: logging.Logger,
) -> None:
    if visible_share_dialog_is_open(page):
        logger.info("Lingering YouTube share dialog detected before choosing Schedule")
        close_visible_share_dialog(page, logger)
    dismiss_overlay_backdrop(page, logger)
    click_first_available(schedule_radio_candidates(page), "Schedule option", logger)
    date_ok = set_schedule_date(page, schedule_time, config, logger)
    if not date_ok:
        logger.warning(
            "Please verify the schedule date manually in YouTube Studio: %s",
            schedule_time.strftime("%Y-%m-%d"),
        )
    time_ok = set_schedule_time(page, schedule_time, config, logger)
    if not time_ok:
        logger.warning(
            "Schedule time mismatch: target=%s selected=%s",
            schedule_time.strftime(config.schedule_time_format),
            selected_schedule_time(page) or "unknown",
        )
        logger.warning(
            "Please verify the schedule time manually in YouTube Studio: %s",
            schedule_time.strftime(config.schedule_time_format),
        )
    if not config.stop_before_publish and (not date_ok or not time_ok):
        raise RuntimeError(
            "Schedule date/time was not set automatically, so Auto Save/Schedule was stopped "
            "to avoid saving the wrong schedule."
        )


def final_button_candidates(page: Page, config: Config) -> list[Locator]:
    if config.schedule_enabled:
        labels = UI_TEXT["schedule"]
    elif config.default_visibility == "public":
        labels = UI_TEXT["publish"]
    else:
        labels = UI_TEXT["save"]
    return button_candidates(page, labels)


def final_action_name(config: Config) -> str:
    if config.schedule_enabled:
        return "Schedule"
    if config.default_visibility == "public":
        return "Publish"
    return "Save"


def visible_share_dialog_is_open(page: Page) -> bool:
    try:
        result = page.evaluate(
            """
            () => {
                const visible = (element) => {
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 0
                        && rect.height > 0
                        && style.display !== "none"
                        && style.visibility !== "hidden";
                };
                return Array.from(document.querySelectorAll("ytcp-video-share-dialog"))
                    .some((dialog) => visible(dialog));
            }
            """,
        )
    except Exception:
        return False
    return bool(result)


def close_visible_share_dialog(page: Page, logger: logging.Logger) -> None:
    for attempt in range(1, 4):
        if not visible_share_dialog_is_open(page):
            dismiss_overlay_backdrop(page, logger)
            return

        try:
            result = page.evaluate(
                """
                ({ closeLabels }) => {
                    const normalize = (value) => (value || "")
                        .normalize("NFD")
                        .replace(/[\\u0300-\\u036f]/g, "")
                        .toLowerCase()
                        .replace(/\\s+/g, " ")
                        .trim();
                    const visible = (element) => {
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0
                            && rect.height > 0
                            && style.display !== "none"
                            && style.visibility !== "hidden";
                    };
                    const textOf = (element) => normalize([
                        element.getAttribute?.("aria-label"),
                        element.getAttribute?.("title"),
                        element.innerText,
                        element.textContent,
                    ].filter(Boolean).join(" "));
                    const clickElement = (element) => {
                        const rect = element.getBoundingClientRect();
                        const centerX = rect.left + rect.width / 2;
                        const centerY = rect.top + rect.height / 2;
                        const target = document.elementFromPoint(centerX, centerY) || element;
                        if (typeof target.click === "function") {
                            target.click();
                        }
                        for (const eventName of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
                            target.dispatchEvent(new MouseEvent(eventName, {
                                bubbles: true,
                                cancelable: true,
                                view: window,
                                clientX: centerX,
                                clientY: centerY,
                            }));
                        }
                    };
                    const labels = closeLabels.map(normalize);
                    const dialog = Array.from(document.querySelectorAll("ytcp-video-share-dialog"))
                        .find((candidate) => visible(candidate));
                    if (!dialog) {
                        return { ok: false, reason: "share dialog not found" };
                    }
                    const dialogRect = dialog.getBoundingClientRect();
                    const buttons = Array.from(dialog.querySelectorAll('[role="button"], button, ytcp-button, tp-yt-paper-button, tp-yt-paper-icon-button'))
                        .filter((candidate) => {
                            const rect = candidate.getBoundingClientRect();
                            const disabled = candidate.disabled
                                || candidate.hasAttribute?.("disabled")
                                || candidate.getAttribute?.("aria-disabled") === "true";
                            return visible(candidate)
                                && !disabled
                                && rect.left >= dialogRect.left - 2
                                && rect.right <= dialogRect.right + 2
                                && rect.top >= dialogRect.top - 2
                                && rect.bottom <= dialogRect.bottom + 2;
                        });
                    const labeledClose = buttons.find((button) => {
                        const text = textOf(button);
                        return labels.some((label) => text.includes(label));
                    });
                    const topRightButton = buttons
                        .filter((button) => {
                            const rect = button.getBoundingClientRect();
                            return rect.top <= dialogRect.top + Math.max(96, dialogRect.height * 0.35);
                        })
                        .sort((left, right) => {
                            const leftRect = left.getBoundingClientRect();
                            const rightRect = right.getBoundingClientRect();
                            if (leftRect.top !== rightRect.top) {
                                return leftRect.top - rightRect.top;
                            }
                            return rightRect.right - leftRect.right;
                        })[0];
                    const closeButton = labeledClose || topRightButton || buttons[buttons.length - 1];
                    if (!closeButton) {
                        return { ok: false, reason: "share dialog close button not found" };
                    }
                    clickElement(closeButton);
                    return {
                        ok: true,
                        text: (closeButton.innerText || closeButton.getAttribute("aria-label") || "").trim(),
                    };
                }
                """,
                {"closeLabels": UI_TEXT["close"]},
            )
            if isinstance(result, dict) and result.get("ok"):
                logger.info(
                    "Clicked YouTube share dialog close button: %s (attempt %s)",
                    result.get("text", ""),
                    attempt,
                )
            elif attempt == 1:
                logger.warning("Could not close share dialog by DOM: %s", result)
        except Exception as error:
            if attempt == 1:
                logger.warning("Could not close share dialog by DOM: %s", error)

        dismiss_overlay_backdrop(page, logger)

        if not visible_share_dialog_is_open(page):
            logger.info("Closed YouTube share dialog")
            human_pause(0.8, 1.5)
            return

        try:
            page.keyboard.press("Escape")
            dismiss_overlay_backdrop(page, logger)
        except Exception as error:
            if attempt == 1:
                logger.warning("Could not dismiss share dialog with Escape: %s", error)

        if not visible_share_dialog_is_open(page):
            logger.info("Closed YouTube share dialog with Escape")
            human_pause(0.8, 1.5)
            return

        human_pause(0.6, 1.0)

    logger.warning("Share dialog is still visible after close attempts")


def upload_dialog_status(page: Page) -> str:
    try:
        result = page.evaluate(
            """
            () => {
                const dialog = document.querySelector("ytcp-uploads-dialog");
                const text = dialog ? (dialog.innerText || dialog.textContent || "") : "";
                return text.replace(/\\s+/g, " ").trim().slice(0, 500);
            }
            """,
        )
    except Exception:
        return ""
    return str(result or "")


def upload_still_running(page: Page) -> bool:
    text = upload_dialog_status(page).lower()
    markers = [
        "uploading",
        "uploaded:",
        "processing",
        "checks",
        "загрузка",
        "загружено:",
        "обработка",
        "проверка",
        "subiendo",
        "procesando",
        "procesamiento",
        "envoi",
        "traitement",
        "hochladen",
        "verarbeitung",
        "carregando",
        "processando",
    ]
    return any(marker in text for marker in markers)


def ready_final_button(
    page: Page,
    config: Config,
    logger: logging.Logger | None = None,
    timeout_ms: int = 600_000,
) -> Locator | None:
    deadline = time.monotonic() + timeout_ms / 1000
    last_status_log = 0.0
    while time.monotonic() < deadline:
        if logger is not None:
            dismiss_video_checks_warning_dialog(page, logger)
        locator = visible_locator(final_button_candidates(page, config), timeout_ms=1_500)
        if locator is not None and not is_disabled(locator):
            return locator
        if logger is not None and time.monotonic() - last_status_log > 30:
            status = upload_dialog_status(page)
            if status:
                logger.info("Waiting for YouTube upload/checks before final action: %s", status[:180])
            last_status_log = time.monotonic()
        time.sleep(0.5)
    return None


def wait_for_final_button(page: Page, config: Config, logger: logging.Logger) -> None:
    locator = ready_final_button(page, config, logger=logger, timeout_ms=20_000)
    if locator is None:
        logger.warning(
            "Final Save/Publish/Schedule button was not detected or stayed disabled. "
            "Please review the browser manually before confirming."
        )
        return
    logger.info("Final %s button is ready; waiting for manual confirmation", final_action_name(config))


def final_success_dialog_is_visible(page: Page) -> bool:
    try:
        result = page.evaluate(
            """
            ({ successLabels }) => {
                const allElements = (root = document) => {
                    const result = [];
                    const visit = (node) => {
                        if (!node) {
                            return;
                        }
                        if (node.nodeType === Node.ELEMENT_NODE) {
                            result.push(node);
                            if (node.shadowRoot) {
                                visit(node.shadowRoot);
                            }
                        }
                        for (const child of Array.from(node.children || [])) {
                            visit(child);
                        }
                    };
                    visit(root.documentElement || root);
                    return result;
                };
                const normalize = (value) => (value || "")
                    .normalize("NFD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .toLowerCase()
                    .replace(/\\s+/g, " ")
                    .trim();
                const visible = (element) => {
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 0
                        && rect.height > 0
                        && style.display !== "none"
                        && style.visibility !== "hidden";
                };
                const textOf = (element) => normalize([
                    element.getAttribute?.("aria-label"),
                    element.getAttribute?.("title"),
                    element.innerText,
                    element.textContent,
                    element.shadowRoot ? element.shadowRoot.textContent : ""
                ].filter(Boolean).join(" "));
                const labels = successLabels.map(normalize);
                const dialogs = allElements().filter((element) => {
                    const tag = element.tagName.toLowerCase();
                    return visible(element)
                        && tag !== "ytcp-uploads-dialog"
                        && (
                            element.getAttribute("role") === "dialog"
                            || tag.includes("dialog")
                            || tag === "ytcp-video-share-dialog"
                        );
                });
                for (const dialog of dialogs) {
                    const text = textOf(dialog);
                    if (labels.some((label) => text.includes(label))) {
                        return { visible: true, text };
                    }
                }
                return { visible: false };
            }
            """,
            {"successLabels": UI_TEXT["final_success"]},
        )
        return isinstance(result, dict) and bool(result.get("visible"))
    except Exception:
        return False


def close_final_success_dialog(page: Page, logger: logging.Logger) -> None:
    for attempt in range(1, 4):
        if not final_success_dialog_is_visible(page):
            dismiss_overlay_backdrop(page, logger)
            return

        try:
            result = page.evaluate(
                """
                ({ successLabels, closeLabels }) => {
                    const allElements = (root = document) => {
                        const result = [];
                        const visit = (node) => {
                            if (!node) {
                                return;
                            }
                            if (node.nodeType === Node.ELEMENT_NODE) {
                                result.push(node);
                                if (node.shadowRoot) {
                                    visit(node.shadowRoot);
                                }
                            }
                            for (const child of Array.from(node.children || [])) {
                                visit(child);
                            }
                        };
                        visit(root.documentElement || root);
                        return result;
                    };
                    const normalize = (value) => (value || "")
                        .normalize("NFD")
                        .replace(/[\\u0300-\\u036f]/g, "")
                        .toLowerCase()
                        .replace(/\\s+/g, " ")
                        .trim();
                    const visible = (element) => {
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0
                            && rect.height > 0
                            && style.display !== "none"
                            && style.visibility !== "hidden";
                    };
                    const textOf = (element) => normalize([
                        element.getAttribute?.("aria-label"),
                        element.getAttribute?.("title"),
                        element.innerText,
                        element.textContent,
                        element.shadowRoot ? element.shadowRoot.textContent : ""
                    ].filter(Boolean).join(" "));
                    const clickElement = (element) => {
                        const rect = element.getBoundingClientRect();
                        const centerX = rect.left + rect.width / 2;
                        const centerY = rect.top + rect.height / 2;
                        const target = document.elementFromPoint(centerX, centerY) || element;
                        if (typeof target.click === "function") {
                            target.click();
                        }
                        for (const eventName of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
                            target.dispatchEvent(new MouseEvent(eventName, {
                                bubbles: true,
                                cancelable: true,
                                view: window,
                                clientX: centerX,
                                clientY: centerY,
                            }));
                        }
                    };
                    const success = successLabels.map(normalize);
                    const close = closeLabels.map(normalize);
                    const dialogs = allElements().filter((element) => {
                        const tag = element.tagName.toLowerCase();
                        return visible(element)
                            && tag !== "ytcp-uploads-dialog"
                            && (
                                element.getAttribute("role") === "dialog"
                                || tag.includes("dialog")
                                || tag === "ytcp-video-share-dialog"
                            );
                    });
                    const dialog = dialogs.find((candidate) => {
                        const text = textOf(candidate);
                        return success.some((label) => text.includes(label));
                    });
                    if (!dialog) {
                        return { ok: false, reason: "success dialog not found" };
                    }
                    const buttons = allElements(dialog).filter((candidate) => {
                        const tag = candidate.tagName.toLowerCase();
                        const looksLikeButton = candidate.getAttribute("role") === "button"
                            || tag === "button"
                            || tag.includes("button");
                        const disabled = candidate.disabled
                            || candidate.hasAttribute("disabled")
                            || candidate.getAttribute("aria-disabled") === "true";
                        return looksLikeButton && visible(candidate) && !disabled;
                    });
                    const closeButton = buttons.find((button) => {
                        const text = textOf(button);
                        return close.some((label) => text.includes(label));
                    }) || buttons[buttons.length - 1];
                    if (!closeButton) {
                        return { ok: false, reason: "close button not found" };
                    }
                    clickElement(closeButton);
                    return {
                        ok: true,
                        text: (closeButton.innerText || closeButton.getAttribute("aria-label") || "").trim(),
                    };
                }
                """,
                {
                    "successLabels": UI_TEXT["final_success"],
                    "closeLabels": UI_TEXT["close"],
                },
            )
            if isinstance(result, dict) and result.get("ok"):
                logger.info(
                    "Clicked YouTube success dialog close button: %s (attempt %s)",
                    result.get("text", ""),
                    attempt,
                )
            elif attempt == 1:
                logger.warning("Could not close success dialog by DOM: %s", result)
        except Exception as error:
            if attempt == 1:
                logger.warning("Could not close success dialog by DOM: %s", error)

        dismiss_overlay_backdrop(page, logger)

        if not final_success_dialog_is_visible(page):
            logger.info("Closed YouTube success dialog")
            human_pause(0.8, 1.5)
            return

        try:
            page.keyboard.press("Escape")
            dismiss_overlay_backdrop(page, logger)
        except Exception as error:
            if attempt == 1:
                logger.warning("Could not dismiss success dialog with Escape: %s", error)

        if not final_success_dialog_is_visible(page):
            logger.info("Closed YouTube success dialog with Escape")
            human_pause(0.8, 1.5)
            return

        human_pause(0.6, 1.0)

    logger.warning("Success dialog is still visible after close attempts")


def click_final_button(page: Page, config: Config, logger: logging.Logger) -> None:
    action_name = final_action_name(config)
    locator = ready_final_button(page, config, logger=logger, timeout_ms=600_000)
    if locator is None:
        raise RuntimeError(f"Final {action_name} button was not ready")
    dismiss_video_checks_warning_dialog(page, logger)
    logger.info("Auto-clicking final %s button", action_name)
    click_locator(locator, f"Final {action_name} button")
    dismiss_video_checks_warning_dialog(page, logger)
    deadline = time.monotonic() + 600
    last_status_log = 0.0
    logger.info("Waiting for YouTube to accept final %s action", action_name)
    while time.monotonic() < deadline:
        if dismiss_video_checks_warning_dialog(page, logger):
            continue
        if final_success_dialog_is_visible(page):
            logger.info("YouTube confirmed final %s action", action_name)
            close_final_success_dialog(page, logger)
            if visible_share_dialog_is_open(page):
                close_visible_share_dialog(page, logger)
            return
        final_button = visible_locator(final_button_candidates(page, config), timeout_ms=1_000)
        if final_button is None:
            human_pause(1.0, 2.0)
            return
        if not is_disabled(final_button):
            logger.info("Final %s button is enabled again; retrying click", action_name)
            click_locator(final_button, f"Final {action_name} button")
            dismiss_video_checks_warning_dialog(page, logger)
            continue
        if time.monotonic() - last_status_log > 30:
            status = upload_dialog_status(page)
            if status:
                logger.info("Still waiting for YouTube to finish upload/checks: %s", status[:180])
            last_status_log = time.monotonic()
        time.sleep(1.0)

    raise RuntimeError(
        f"YouTube did not accept the final {action_name} action. "
        "The upload dialog is still open after waiting 10 minutes, so the files were not archived."
    )


def take_screenshot(page: Page, logs_folder: Path, stem: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = logs_folder / f"{stem}_{timestamp}.png"
    page.screenshot(path=str(screenshot_path), full_page=True)
    return screenshot_path


def unique_destination(folder: Path, filename: str) -> Path:
    destination = folder / filename
    if not destination.exists():
        return destination

    stem = destination.stem
    suffix = destination.suffix
    for index in range(1, 10_000):
        candidate = folder / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Could not create a unique destination for {filename}")


def move_pair(pair: VideoPair, destination_folder: Path, logger: logging.Logger) -> dict[str, str]:
    destination_folder.mkdir(parents=True, exist_ok=True)
    moved_paths: dict[str, str] = {}
    for source in [pair.video_path, pair.metadata_path]:
        original_source = str(source)
        if not source.exists():
            logger.warning(
                "Source file is already missing after YouTube accepted the upload: %s",
                source,
            )
            moved_paths[original_source] = ""
            continue
        destination = unique_destination(destination_folder, source.name)
        shutil.move(str(source), str(destination))
        moved_paths[original_source] = str(destination)
        logger.info("Moved %s -> %s", source.name, destination)
    return moved_paths


def write_uploaded_report(
    pair: VideoPair,
    metadata: Metadata,
    config: Config,
    schedule_time: datetime | None,
    moved_paths: dict[str, str],
    logger: logging.Logger,
) -> dict[str, str]:
    report_path = config.logs_folder / "uploaded_report.csv"
    row = {
        "confirmed_at": datetime.now().isoformat(timespec="seconds"),
        "source_video": str(pair.video_path),
        "source_metadata": str(pair.metadata_path),
        "archived_video": moved_paths.get(str(pair.video_path), ""),
        "archived_metadata": moved_paths.get(str(pair.metadata_path), ""),
        "title": metadata.title,
        "visibility": "schedule" if config.schedule_enabled else config.default_visibility,
        "schedule_time": schedule_time.strftime("%Y-%m-%d %H:%M") if schedule_time else "",
    }
    fields = list(row.keys())
    write_header = not report_path.exists()
    with report_path.open("a", newline="", encoding="utf-8-sig") as report_file:
        writer = csv.DictWriter(report_file, fieldnames=fields)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    logger.info("Uploaded report updated: %s", report_path)
    return row


def try_close_upload_dialog(page: Page, logger: logging.Logger) -> None:
    if visible_share_dialog_is_open(page):
        close_visible_share_dialog(page, logger)
        return
    if final_success_dialog_is_visible(page):
        close_final_success_dialog(page, logger)
        return

    try:
        page.keyboard.press("Escape")
        human_pause(0.8, 1.5)
        discard_locator = visible_locator(button_candidates(page, UI_TEXT["discard"]), timeout_ms=2_000)
        if discard_locator is not None:
            click_locator(discard_locator, "Discard upload dialog")
    except Exception as error:
        logger.warning("Could not close upload dialog automatically: %s", error)


def launch_chrome(playwright: Playwright, config: Config) -> tuple[BrowserContext, Browser | None]:
    global LAUNCHED_CHROME_PROCESS

    if config.chrome_user_data_dir:
        existing_browser = try_connect_existing_chrome(playwright)
        if existing_browser is not None:
            LAUNCHED_CHROME_PROCESS = None
            return existing_browser
        raise RuntimeError(
            "Open YouTube channel first and keep that Chrome window open. "
            "The uploader now attaches only to the already open manual Chrome session "
            "to avoid Chrome restore prompts and profile corruption."
        )

    browser = playwright.chromium.launch(
        headless=False,
        channel="chrome",
        args=chrome_launch_args(config.youtube_studio_url),
    )
    context = browser.new_context(viewport=None)
    return context, browser


def close_launched_chrome_gracefully(browser: Browser | None, logger: logging.Logger) -> None:
    global LAUNCHED_CHROME_PROCESS

    process = LAUNCHED_CHROME_PROCESS
    LAUNCHED_CHROME_PROCESS = None

    if browser is not None:
        try:
            session = browser.new_browser_cdp_session()
            session.send("Browser.close")
        except Exception as error:
            logger.debug("Could not close temporary Chrome via CDP Browser.close: %s", error)
        finally:
            try:
                browser.close()
            except Exception:
                pass

    if process is None:
        return

    try:
        process.wait(timeout=10)
        logger.info("Closed temporary Chrome window gracefully")
        return
    except Exception:
        logger.warning(
            "Temporary Chrome window did not exit in time. Leaving it open to avoid Chrome restore prompt."
        )


def load_playwright() -> Callable:
    global PlaywrightTimeoutError, sync_playwright

    if sync_playwright is not None:
        return sync_playwright

    try:
        from playwright.sync_api import (  # pylint: disable=import-outside-toplevel
            TimeoutError as ImportedPlaywrightTimeoutError,
            sync_playwright as imported_sync_playwright,
        )
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install -r requirements.txt "
            "and then playwright install"
        ) from error

    PlaywrightTimeoutError = ImportedPlaywrightTimeoutError
    sync_playwright = imported_sync_playwright
    return imported_sync_playwright


def get_or_create_page(context: BrowserContext) -> Page:
    deadline = time.time() + 15
    while time.time() < deadline:
        for page in context.pages:
            if page.is_closed():
                continue
            url = page.url or ""
            if "studio.youtube.com" in url or "youtube.com" in url:
                return page
        if context.pages:
            first_open_page = next((page for page in context.pages if not page.is_closed()), None)
            if first_open_page is not None:
                return first_open_page
        time.sleep(0.25)
    return context.new_page()


def prepare_upload(
    page: Page,
    pair: VideoPair,
    metadata: Metadata,
    config: Config,
    logger: logging.Logger,
    schedule_time: datetime | None,
) -> None:
    logger.info("Opening YouTube Studio")
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
    except Exception:
        pass
    human_pause(1.5, 3.0)
    switch_youtube_channel_if_needed(page, config, logger)

    open_create_menu(page, logger)
    click_upload_videos(page, logger)
    set_video_file(page, pair.video_path, logger)
    ensure_upload_is_available(page, logger)

    logger.info("Filling metadata for %s", pair.video_path.name)
    fill_title_and_description(page, metadata, logger)
    select_made_for_kids(page, config.made_for_kids, logger)
    advance_to_visibility_screen(page, config, logger)

    if config.schedule_enabled:
        if schedule_time is None:
            raise RuntimeError("schedule_time is required when schedule is enabled")
        apply_schedule(page, schedule_time, config, logger)
    else:
        select_visibility(page, config.default_visibility, logger)

    if config.stop_before_publish:
        wait_for_final_button(page, config, logger)
    else:
        click_final_button(page, config, logger)


def confirmation_prompt(
    pair: VideoPair,
    metadata: Metadata,
    config: Config,
    schedule_time: datetime | None,
) -> str:
    print()
    print("=" * 72)
    print(f"Video: {pair.video_path.name}")
    print(f"Title: {metadata.title}")
    selected_visibility = "schedule" if config.schedule_enabled else config.default_visibility
    print(f"Selected visibility: {selected_visibility}")
    if schedule_time is not None:
        print(f"Schedule time: {schedule_time.strftime('%Y-%m-%d %H:%M')}")
    print()
    print("The browser is waiting on the final Save/Publish/Schedule screen.")
    print("The script will NOT click the final button automatically.")
    answer = input(
        "Press Enter after you manually confirm publishing/scheduling, "
        "or type skip/error: "
    )
    return answer.strip().lower()


def process_pair(
    page: Page,
    pair: VideoPair,
    index: int,
    config: Config,
    logger: logging.Logger,
    schedule_start: datetime | None,
    confirmation_callback: ConfirmationCallback | None = None,
    uploaded_callback: UploadedCallback | None = None,
) -> None:
    metadata = parse_metadata(pair.metadata_path)
    schedule_time = schedule_time_for_index(config, index, schedule_start)

    logger.info("Processing %s", pair.video_path.name)
    logger.info("Title: %s", metadata.title)

    try:
        prepare_upload(page, pair, metadata, config, logger, schedule_time)
        if config.stop_before_publish:
            prompt = confirmation_callback or confirmation_prompt
            answer = prompt(pair, metadata, config, schedule_time)
        else:
            answer = ""

        if answer == "":
            moved_paths = move_pair(pair, config.uploaded_folder, logger)
            report_row = write_uploaded_report(
                pair,
                metadata,
                config,
                schedule_time,
                moved_paths,
                logger,
            )
            if uploaded_callback is not None:
                uploaded_callback(report_row)
            logger.info("Marked as uploaded: %s", pair.video_path.name)
        elif answer == "skip":
            logger.info("Skipped by user: %s", pair.video_path.name)
            try_close_upload_dialog(page, logger)
        elif answer == "error":
            screenshot_path = take_screenshot(page, config.logs_folder, pair.video_path.stem)
            logger.error("Marked as error by user. Screenshot: %s", screenshot_path)
            if config.move_failed_to_failed:
                move_pair(pair, config.failed_folder, logger)
            try_close_upload_dialog(page, logger)
        else:
            logger.warning("Unknown answer '%s'; leaving files in videos folder", answer)
            try_close_upload_dialog(page, logger)
    except Exception as error:
        logger.exception("Failed to process %s: %s", pair.video_path.name, error)
        try:
            screenshot_path = take_screenshot(page, config.logs_folder, pair.video_path.stem)
            logger.error("Failure screenshot saved: %s", screenshot_path)
        except Exception as screenshot_error:
            logger.error("Could not save failure screenshot: %s", screenshot_error)

        try_close_upload_dialog(page, logger)
        raise


def run_uploads(
    config: Config,
    dry_run: bool = False,
    confirmation_callback: ConfirmationCallback | None = None,
    uploaded_callback: UploadedCallback | None = None,
    stop_event: threading.Event | None = None,
    log_callback: LogCallback | None = None,
) -> None:
    ensure_directories(config)
    logger = setup_logging(config.logs_folder, log_callback=log_callback)

    if not config.stop_before_publish and not config.schedule_enabled and config.default_visibility == "public":
        raise ValueError(
            "Auto final action is disabled for immediate public publishing. "
            "Turn on Schedule or enable manual final confirmation."
        )

    if not config.stop_before_publish:
        logger.warning(
            "Auto final action is enabled. The uploader will click final Save/Schedule "
            "for queued videos after filling the upload form."
        )

    pairs = find_video_pairs(config.video_folder, logger)
    all_pairs_count = len(pairs)
    pairs = limit_video_pairs(pairs, config)
    if not pairs:
        logger.info("No .mp4 + metadata pairs found in %s", config.video_folder)
        return

    schedule_start = parse_schedule_start(config.schedule_start_datetime) if config.schedule_enabled else None

    logger.info(
        "Found %s video pair(s); queued %s for this run",
        all_pairs_count,
        len(pairs),
    )
    if config.max_videos_per_run > 0:
        logger.info("Run limit: queued up to %s video(s)", config.max_videos_per_run)
    schedule_index_offset = 0

    if dry_run:
        for index, pair in enumerate(pairs):
            if stop_event is not None and stop_event.is_set():
                logger.info("Stop requested; dry-run halted")
                break
            metadata = parse_metadata(pair.metadata_path)
            schedule_time = schedule_time_for_index(config, schedule_index_offset + index, schedule_start)
            logger.info(
                "DRY RUN | %s | title=%s | visibility=%s | schedule=%s",
                pair.video_path.name,
                metadata.title,
                "schedule" if config.schedule_enabled else config.default_visibility,
                schedule_time.strftime("%Y-%m-%d %H:%M") if schedule_time else "",
            )
        return

    playwright_runner = load_playwright()
    with playwright_runner() as playwright:
        context, browser = launch_chrome(playwright, config)
        launched_chrome_process = LAUNCHED_CHROME_PROCESS is not None
        try:
            page = get_or_create_page(context)
            for index, pair in enumerate(pairs):
                if stop_event is not None and stop_event.is_set():
                    logger.info("Stop requested; no more videos will be started")
                    break
                process_pair(
                    page,
                    pair,
                    schedule_index_offset + index,
                    config,
                    logger,
                    schedule_start,
                    confirmation_callback=confirmation_callback,
                    uploaded_callback=uploaded_callback,
                )
        finally:
            if launched_chrome_process:
                try:
                    context.close()
                except Exception:
                    pass
                close_launched_chrome_gracefully(browser, logger)
            elif browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Semi-manual YouTube Shorts uploader")
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help="Path to config.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse config and metadata without opening Chrome",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(Path(args.config).resolve())
    run_uploads(config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
