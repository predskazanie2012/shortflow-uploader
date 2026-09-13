from __future__ import annotations

import ctypes
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

try:
    import winsound
except ImportError:
    winsound = None

import uploader


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
ICON_PATH = BASE_DIR / "assets" / "shortflow_icon.ico"
ICON_PNG_PATH = BASE_DIR / "assets" / "shortflow_icon.png"
APP_USER_MODEL_ID = "ShortFlow.Uploader.Desktop.1"


STANDARD_CHANNEL_PRESET = "standard"
STANDARD_CHANNEL_DAILY_TIMES = "09:00, 14:00, 19:00"
STANDARD_VIDEOS_PER_DAY = 3
STANDARD_BATCH_DAYS = 1
STANDARD_BATCH_LIMIT = STANDARD_VIDEOS_PER_DAY * STANDARD_BATCH_DAYS
UNVERIFIED_CHANNEL_PRESETS: set[str] = set()
CHANNEL_PRESETS = {
    STANDARD_CHANNEL_PRESET: {
        "label": "Обычный канал",
        "hint": "Обычный режим: 3 видео в день, очередь на 1 день вперед, автослоты 09:00, 14:00, 19:00.",
    },
    "ja": {
        "label": "Japanese - 1 видео/день",
        "hint": "\u041d\u0435\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u043d\u044b\u0439 \u043a\u0430\u043d\u0430\u043b: 1 \u0432\u0438\u0434\u0435\u043e \u0432 \u0434\u0435\u043d\u044c, \u0441\u043b\u043e\u0442 09:15.",
        "max_videos": 1,
        "start_time": "09:15",
    },
    "hi": {
        "label": "Hindi - 1 видео/день",
        "hint": "\u041d\u0435\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u043d\u044b\u0439 \u043a\u0430\u043d\u0430\u043b: 1 \u0432\u0438\u0434\u0435\u043e \u0432 \u0434\u0435\u043d\u044c, \u0441\u043b\u043e\u0442 09:45.",
        "max_videos": 1,
        "start_time": "09:45",
    },
    "ar": {
        "label": "Arabic - 1 видео/день",
        "hint": "\u041d\u0435\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u043d\u044b\u0439 \u043a\u0430\u043d\u0430\u043b: 1 \u0432\u0438\u0434\u0435\u043e \u0432 \u0434\u0435\u043d\u044c, \u0441\u043b\u043e\u0442 09:30.",
        "max_videos": 1,
        "start_time": "09:30",
    },
    "zh": {
        "label": "Chinese - 1 видео/день",
        "hint": "\u041d\u0435\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u043d\u044b\u0439 \u043a\u0430\u043d\u0430\u043b: 1 \u0432\u0438\u0434\u0435\u043e \u0432 \u0434\u0435\u043d\u044c, \u0441\u043b\u043e\u0442 09:00.",
        "max_videos": 1,
        "start_time": "09:00",
    },
}
# Legacy single-video presets stay here only for backward compatibility with
# older saved configs; all channels now use the standard 3-video preset.
for legacy_standard_key in ("ja", "zh", "ar", "hi"):
    CHANNEL_PRESETS.pop(legacy_standard_key, None)
CHANNEL_PRESET_LABELS = [preset["label"] for preset in CHANNEL_PRESETS.values()]
CHANNEL_PRESET_BY_LABEL = {preset["label"]: key for key, preset in CHANNEL_PRESETS.items()}


COLORS = {
    "page": "#f7f9fc",
    "panel": "#ffffff",
    "panel_alt": "#f8fafc",
    "text": "#111827",
    "muted": "#64748b",
    "border": "#e5e7eb",
    "accent": "#ff1744",
    "accent_hover": "#e11d48",
    "accent_soft": "#fff1f3",
    "purple": "#7c3aed",
    "purple_soft": "#f3e8ff",
    "success": "#16a34a",
    "success_soft": "#eafaf0",
    "danger": "#dc2626",
    "danger_soft": "#fee2e2",
    "blue": "#2563eb",
    "blue_soft": "#eff6ff",
    "warning_soft": "#fff7ed",
}


class ScanLogger:
    def __init__(self, app: "UploaderGUI") -> None:
        self.app = app

    def warning(self, message: str, *args: object) -> None:
        self.app.append_log("WARNING | " + (message % args))


class UploaderGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.uploaded_this_run = 0
        self.run_total_videos = 0
        self.worker_had_error = False
        self.reset_auto_slot_offset = False
        self.channel_process: subprocess.Popen | None = None
        self.logo_image: tk.PhotoImage | None = None
        self.source_refresh_job: str | None = None

        self.source_folder_var = tk.StringVar(value="")
        self.chrome_profile_var = tk.StringVar(value="chrome-profile")
        self.channel_preset_var = tk.StringVar(value=CHANNEL_PRESETS[STANDARD_CHANNEL_PRESET]["label"])
        self.channel_preset_hint_var = tk.StringVar(value=CHANNEL_PRESETS[STANDARD_CHANNEL_PRESET]["hint"])
        self.batch_limit_var = tk.StringVar(value=str(STANDARD_BATCH_LIMIT))
        self.schedule_enabled_var = tk.BooleanVar(value=False)
        self.manual_schedule_var = tk.BooleanVar(value=False)
        self.schedule_start_var = tk.StringVar(value="")
        self.schedule_times_var = tk.StringVar(value="10:00, 15:00, 20:00")
        self.auto_slot_hint_var = tk.StringVar(value="Auto: +15 min after each 3 videos")
        self.status_var = tk.StringVar(value="Ready")
        self.summary_var = tk.StringVar(value="")
        self.progress_var = tk.StringVar(value="Готово: выберите папку с видео")
        self.source_count_var = tk.StringVar(value="0 видео найдено")

        self.configure_window()
        self.build_ui()
        self.load_saved_settings()
        self.preview_queue(show_message=False)
        self.root.after(100, self.poll_ui_queue)

    def configure_window(self) -> None:
        self.root.title("ShortFlow")
        self.root.geometry("1320x820")
        self.root.minsize(1060, 680)
        self.root.configure(background=COLORS["page"])
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        if ICON_PATH.exists():
            try:
                self.root.iconbitmap(default=str(ICON_PATH))
            except tk.TclError:
                pass

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("TFrame", background=COLORS["page"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure(
            "HeaderTitle.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            font=("Segoe UI", 17, "bold"),
        )
        style.configure(
            "HeaderSub.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 10),
        )
        style.configure("TLabel", background=COLORS["page"], foreground=COLORS["text"], font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=COLORS["page"], foreground=COLORS["muted"], font=("Segoe UI", 9))
        style.configure("PanelMuted.TLabel", background=COLORS["panel"], foreground=COLORS["muted"], font=("Segoe UI", 9))
        style.configure(
            "Status.TLabel",
            background=COLORS["page"],
            foreground=COLORS["success"],
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "Panel.TLabelframe",
            background=COLORS["page"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Panel.TLabelframe.Label",
            background=COLORS["page"],
            foreground=COLORS["text"],
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "TButton",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            focusthickness=0,
            font=("Segoe UI", 10),
            padding=(12, 7),
        )
        style.map("TButton", background=[("active", COLORS["panel_alt"])])
        style.configure(
            "Primary.TButton",
            background=COLORS["accent"],
            foreground="#ffffff",
            bordercolor=COLORS["accent"],
            focusthickness=0,
            font=("Segoe UI", 10, "bold"),
            padding=(22, 11),
        )
        style.map(
            "Primary.TButton",
            background=[("active", COLORS["accent_hover"]), ("disabled", "#cbd5e1")],
            foreground=[("disabled", "#ffffff")],
        )
        style.configure(
            "Danger.TButton",
            background=COLORS["danger_soft"],
            foreground=COLORS["danger"],
            bordercolor="#fda29b",
            font=("Segoe UI", 10),
            padding=(12, 7),
        )
        style.map("Danger.TButton", background=[("active", "#ffd7d2")])
        style.configure("TCheckbutton", background=COLORS["page"], foreground=COLORS["text"], font=("Segoe UI", 10))
        style.configure(
            "TEntry",
            fieldbackground=COLORS["panel"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            insertcolor=COLORS["text"],
            padding=(6, 4),
        )
        style.configure(
            "Treeview",
            background=COLORS["panel"],
            fieldbackground=COLORS["panel"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            font=("Segoe UI", 9),
            rowheight=42,
        )
        style.configure(
            "Treeview.Heading",
            background="#f8fafc",
            foreground=COLORS["muted"],
            bordercolor=COLORS["border"],
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "Treeview",
            background=[("selected", COLORS["accent_soft"])],
            foreground=[("selected", COLORS["text"])],
        )

    def build_ui(self) -> None:
        def card(parent: tk.Widget, **grid: object) -> tk.Frame:
            frame = tk.Frame(
                parent,
                bg=COLORS["panel"],
                highlightbackground=COLORS["border"],
                highlightthickness=1,
                bd=0,
            )
            frame.grid(**grid)
            return frame

        def label(parent: tk.Widget, text: str = "", **kwargs: object) -> tk.Label:
            options = {
                "bg": kwargs.pop("bg", parent.cget("bg") if hasattr(parent, "cget") else COLORS["panel"]),
                "fg": kwargs.pop("fg", COLORS["text"]),
                "font": kwargs.pop("font", ("Segoe UI", 10)),
                "anchor": kwargs.pop("anchor", "w"),
                "justify": kwargs.pop("justify", "left"),
            }
            options.update(kwargs)
            return tk.Label(parent, text=text, **options)

        page = tk.Frame(self.root, bg=COLORS["page"])
        page.grid(row=0, column=0, sticky="nsew")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)

        header = tk.Frame(page, bg=COLORS["page"])
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 8))
        header.columnconfigure(1, weight=1)
        if ICON_PNG_PATH.exists():
            try:
                self.logo_image = tk.PhotoImage(file=str(ICON_PNG_PATH)).subsample(5, 5)
                label(header, image=self.logo_image, bg=COLORS["page"]).grid(row=0, column=0, rowspan=2, padx=(0, 10))
            except tk.TclError:
                label(header, text="▶", bg=COLORS["page"], fg=COLORS["accent"], font=("Segoe UI", 22, "bold")).grid(
                    row=0, column=0, rowspan=2, padx=(0, 10)
                )
        else:
            label(header, text="▶", bg=COLORS["page"], fg=COLORS["accent"], font=("Segoe UI", 22, "bold")).grid(
                row=0, column=0, rowspan=2, padx=(0, 10)
            )
        label(header, text="ShortFlow", bg=COLORS["page"], font=("Segoe UI", 18, "bold")).grid(
            row=0, column=1, sticky="w"
        )
        label(
            header,
            text="Выберите папку, откройте нужный канал в Chrome и нажмите старт.",
            bg=COLORS["page"],
            fg=COLORS["muted"],
            font=("Segoe UI", 10),
        ).grid(row=1, column=1, sticky="w", pady=(2, 0))

        controls = card(page, row=1, column=0, sticky="ew", padx=20, pady=(0, 12), ipadx=16, ipady=14)
        controls.columnconfigure(1, weight=1)

        label(controls, text="Папка с видео", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 8)
        )
        self.source_folder_entry = ttk.Entry(controls, textvariable=self.source_folder_var, width=74)
        self.source_folder_entry.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=(0, 8))
        self.source_folder_entry.bind("<Return>", lambda _event: self.refresh_source_from_entry(show_errors=True))
        self.source_folder_entry.bind("<FocusOut>", lambda _event: self.refresh_source_from_entry(show_errors=False))
        self.source_folder_entry.bind("<KeyRelease>", lambda _event: self.schedule_source_entry_refresh())
        self.source_folder_entry.bind("<<Paste>>", lambda _event: self.schedule_source_entry_refresh(delay_ms=120))
        ttk.Button(controls, text="Вставить", command=self.paste_source_folder_from_clipboard).grid(
            row=0, column=2, sticky="ew", padx=(0, 8), pady=(0, 8)
        )
        ttk.Button(controls, text="Применить", command=lambda: self.refresh_source_from_entry(show_errors=True)).grid(
            row=0, column=3, sticky="ew", padx=(0, 8), pady=(0, 8)
        )
        ttk.Button(controls, text="Выбрать", command=self.browse_source_folder).grid(
            row=0, column=4, sticky="ew", padx=(0, 16), pady=(0, 8)
        )
        label(controls, textvariable=self.source_count_var, fg=COLORS["accent"], font=("Segoe UI", 9, "bold")).grid(
            row=1, column=1, sticky="w", pady=(0, 10)
        )

        ttk.Button(controls, text="Открыть YouTube канал", command=self.open_chrome_profile_for_login).grid(
            row=2, column=0, sticky="ew", padx=(0, 10), pady=(0, 8)
        )
        channel_actions = tk.Frame(controls, bg=COLORS["panel"])
        channel_actions.grid(row=2, column=1, columnspan=6, sticky="w", pady=(0, 8))
        label(channel_actions, text="Режим", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.channel_preset_combo = ttk.Combobox(
            channel_actions,
            textvariable=self.channel_preset_var,
            values=CHANNEL_PRESET_LABELS,
            state="readonly",
            width=28,
        )
        self.channel_preset_combo.grid(row=0, column=1, sticky="w", padx=(0, 18))
        self.channel_preset_combo.bind("<<ComboboxSelected>>", lambda _event: self.on_channel_preset_changed())
        label(
            channel_actions,
            text="Канал переключается вручную в открытом Chrome.",
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).grid(row=0, column=2, sticky="w", padx=(0, 22))
        label(channel_actions, text="Видео", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=3, sticky="w", padx=(0, 8)
        )
        self.batch_limit_entry = ttk.Entry(channel_actions, textvariable=self.batch_limit_var, width=7)
        self.batch_limit_entry.grid(
            row=0, column=4, sticky="w", padx=(0, 18)
        )
        self.start_button = ttk.Button(channel_actions, text="▶ Старт", style="Primary.TButton", command=self.start_upload)
        self.start_button.grid(row=0, column=5, sticky="ew", padx=(0, 10))
        self.stop_button = ttk.Button(channel_actions, text="Стоп", command=self.stop_after_current, state=tk.DISABLED)
        self.stop_button.grid(row=0, column=6, sticky="ew")
        label(
            channel_actions,
            textvariable=self.channel_preset_hint_var,
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).grid(row=1, column=0, columnspan=7, sticky="w", pady=(8, 0))

        self.manual_schedule_check = ttk.Checkbutton(
            controls,
            text="Своя дата и время",
            variable=self.manual_schedule_var,
            command=self.on_manual_schedule_changed,
        )
        self.manual_schedule_check.grid(row=3, column=0, sticky="w", padx=(0, 10), pady=(4, 0))
        schedule_actions = tk.Frame(controls, bg=COLORS["panel"])
        schedule_actions.grid(row=3, column=1, columnspan=6, sticky="w", pady=(4, 0))
        self.schedule_start_entry = ttk.Entry(schedule_actions, textvariable=self.schedule_start_var, width=18)
        self.schedule_start_entry.grid(row=0, column=0, sticky="w", padx=(0, 18))
        label(schedule_actions, text="Время в день", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=1, sticky="w", padx=(0, 8)
        )
        self.schedule_times_entry = ttk.Entry(schedule_actions, textvariable=self.schedule_times_var, width=20)
        self.schedule_times_entry.grid(row=0, column=2, sticky="w", padx=(0, 10))
        self.morning_plan_button = ttk.Button(schedule_actions, text="План 09:00", command=self.use_morning_plan)
        self.morning_plan_button.grid(row=0, column=3, sticky="ew")

        queue_card = card(page, row=2, column=0, sticky="nsew", padx=20, pady=(0, 10), ipadx=14, ipady=14)
        queue_card.columnconfigure(0, weight=1)
        queue_card.rowconfigure(1, weight=1)
        label(queue_card, text="Очередь", font=("Segoe UI", 15, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 10))
        label(queue_card, textvariable=self.summary_var, fg=COLORS["muted"], font=("Segoe UI", 9)).grid(
            row=0,
            column=1,
            sticky="e",
            pady=(0, 10),
        )

        columns = ("num", "file", "title", "schedule", "status")
        self.video_tree = ttk.Treeview(queue_card, columns=columns, show="headings", selectmode="browse")
        self.video_tree.tag_configure("even", background=COLORS["panel"])
        self.video_tree.tag_configure("odd", background=COLORS["panel_alt"])
        self.video_tree.tag_configure("error", background=COLORS["danger_soft"])
        self.video_tree.heading("num", text="#")
        self.video_tree.heading("file", text="Файл")
        self.video_tree.heading("title", text="Заголовок")
        self.video_tree.heading("schedule", text="Расписание")
        self.video_tree.heading("status", text="Статус")
        self.video_tree.column("num", width=46, minwidth=40, stretch=False, anchor="center")
        self.video_tree.column("file", width=190, minwidth=160, stretch=False)
        self.video_tree.column("title", width=520, minwidth=330, stretch=True)
        self.video_tree.column("schedule", width=150, minwidth=120, stretch=False, anchor="center")
        self.video_tree.column("status", width=110, minwidth=90, stretch=False, anchor="center")
        self.video_tree.grid(row=1, column=0, columnspan=2, sticky="nsew")

        tree_scroll = ttk.Scrollbar(queue_card, orient=tk.VERTICAL, command=self.video_tree.yview)
        tree_scroll.grid(row=1, column=2, sticky="ns")
        self.video_tree.configure(yscrollcommand=tree_scroll.set)

        footer = tk.Frame(page, bg=COLORS["page"])
        footer.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 12))
        footer.columnconfigure(0, weight=1)
        label(footer, textvariable=self.progress_var, bg=COLORS["page"], fg=COLORS["success"], font=("Segoe UI", 10, "bold")).grid(
            row=0,
            column=0,
            sticky="w",
        )
        label(footer, text="v1.9", bg=COLORS["page"], fg=COLORS["muted"], font=("Segoe UI", 9)).grid(
            row=0,
            column=1,
            sticky="e",
        )

    def load_raw_config(self) -> dict:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        config = uploader.load_config(CONFIG_PATH)
        return {
            "youtube_studio_url": config.youtube_studio_url,
            "video_folder": str(config.video_folder),
            "uploaded_folder": str(config.uploaded_folder),
            "failed_folder": str(config.failed_folder),
            "logs_folder": str(config.logs_folder),
            "default_visibility": config.default_visibility,
            "made_for_kids": config.made_for_kids,
            "stop_before_publish": config.stop_before_publish,
            "chrome_user_data_dir": config.chrome_user_data_dir,
            "active_channel": config.active_channel,
            "channel_preset": STANDARD_CHANNEL_PRESET,
            "max_videos_per_run": config.max_videos_per_run,
            "schedule_enabled": config.schedule_enabled,
            "manual_schedule_enabled": config.manual_schedule_enabled,
            "schedule_start_datetime": config.schedule_start_datetime,
            "schedule_interval_hours": config.schedule_interval_hours,
            "schedule_daily_times": config.schedule_daily_times,
            "schedule_language_slot": config.schedule_language_slot,
            "schedule_slot_offset_minutes": config.schedule_slot_offset_minutes,
            "schedule_date_format": config.schedule_date_format,
            "schedule_time_format": config.schedule_time_format,
            "move_failed_to_failed": config.move_failed_to_failed,
            "max_next_clicks": config.max_next_clicks,
        }

    def save_raw_config(self, data: dict) -> None:
        CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def channel_preset_key(self) -> str:
        label = self.channel_preset_var.get().strip()
        return CHANNEL_PRESET_BY_LABEL.get(label, STANDARD_CHANNEL_PRESET)

    def set_channel_preset_key(self, key: str) -> None:
        if key not in CHANNEL_PRESETS:
            key = STANDARD_CHANNEL_PRESET
        self.channel_preset_var.set(CHANNEL_PRESETS[key]["label"])
        self.channel_preset_hint_var.set(CHANNEL_PRESETS[key]["hint"])

    def is_unverified_channel_preset(self) -> bool:
        return self.channel_preset_key() in UNVERIFIED_CHANNEL_PRESETS

    def one_video_daily_start(self, first_time: str = "09:00") -> str:
        now = datetime.now()
        schedule_start = self.schedule_start_var.get().strip()
        if schedule_start:
            try:
                parsed = uploader.parse_schedule_start(schedule_start)
                if parsed > now:
                    return parsed.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass
        tomorrow = now + timedelta(days=1)
        return f"{tomorrow.strftime('%Y-%m-%d')} {first_time}"

    def load_saved_settings(self) -> None:
        try:
            raw_config = self.load_raw_config()
        except Exception as error:
            self.append_log(f"ERROR | {error}")
            return
        preset_key = str(raw_config.get("channel_preset") or raw_config.get("active_channel") or STANDARD_CHANNEL_PRESET)
        self.set_channel_preset_key(preset_key)
        self.source_folder_var.set(str(raw_config.get("video_folder") or "videos"))
        self.chrome_profile_var.set(str(raw_config.get("chrome_user_data_dir") or "chrome-profile"))
        max_videos = int(raw_config.get("max_videos_per_run") or 0)
        self.batch_limit_var.set(str(max_videos) if max_videos > 0 else "All")
        self.schedule_enabled_var.set(True)
        self.manual_schedule_var.set(bool(raw_config.get("manual_schedule_enabled", False)))
        self.schedule_start_var.set(str(raw_config.get("schedule_start_datetime") or ""))
        self.update_auto_slot_hint(raw_config)

        daily_times = raw_config.get("schedule_daily_times")
        if self.is_unverified_channel_preset():
            self.batch_limit_var.set(
                str(CHANNEL_PRESETS[self.channel_preset_key()].get("max_videos", STANDARD_VIDEOS_PER_DAY))
            )
            self.schedule_times_var.set("1 раз в день")
        elif isinstance(daily_times, list) and daily_times:
            self.schedule_times_var.set(", ".join(str(item) for item in daily_times))
        elif isinstance(daily_times, list):
            self.schedule_times_var.set(STANDARD_CHANNEL_DAILY_TIMES)
        else:
            self.schedule_times_var.set(str(daily_times or STANDARD_CHANNEL_DAILY_TIMES))
        self.sync_schedule_controls()

    def clean_path_text(self, value: str) -> str:
        return value.strip().strip('"').strip("'").strip()

    def source_folder_from_entry(self) -> Path:
        value = self.clean_path_text(self.source_folder_var.get())
        if not value:
            raise ValueError("Folder is empty")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        return path

    def schedule_source_entry_refresh(self, delay_ms: int = 700) -> None:
        if self.source_refresh_job is not None:
            try:
                self.root.after_cancel(self.source_refresh_job)
            except Exception:
                pass
        self.source_refresh_job = self.root.after(
            delay_ms,
            lambda: self.refresh_source_from_entry(show_errors=False),
        )

    def refresh_source_from_entry(self, show_errors: bool = False) -> None:
        self.source_refresh_job = None
        try:
            source_folder = self.source_folder_from_entry()
            if not source_folder.exists() or not source_folder.is_dir():
                if show_errors:
                    messagebox.showerror("Папка не найдена", f"Такой папки нет:\n{source_folder}")
                return
            self.source_folder_var.set(str(source_folder.resolve()))
            self.preview_queue(show_message=show_errors)
        except Exception as error:
            if show_errors:
                messagebox.showerror("Не удалось открыть папку", str(error))

    def paste_source_folder_from_clipboard(self) -> None:
        try:
            value = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showerror("Буфер пуст", "В буфере обмена нет пути к папке.")
            return
        self.source_folder_var.set(self.clean_path_text(value))
        self.source_folder_entry.focus_set()
        self.source_folder_entry.icursor(tk.END)
        self.refresh_source_from_entry(show_errors=True)

    def browse_source_folder(self) -> None:
        initial_text = self.clean_path_text(self.source_folder_var.get())
        initial_path = Path(initial_text).expanduser() if initial_text else BASE_DIR
        if not initial_path.is_absolute():
            initial_path = BASE_DIR / initial_path
        if initial_path.is_file():
            initial_path = initial_path.parent
        selected = filedialog.askdirectory(
            title="Select any folder with videos and metadata",
            initialdir=str(initial_path) if initial_path.exists() else str(BASE_DIR),
        )
        if selected:
            self.source_folder_var.set(selected)
            self.append_log(f"INFO | Source folder selected: {selected}")
            self.refresh_source_from_entry(show_errors=True)

    def browse_chrome_profile(self) -> None:
        initial = self.chrome_profile_var.get().strip() or str(BASE_DIR / "chrome-profile")
        selected = filedialog.askdirectory(
            title="Select or create a Chrome profile folder for this uploader",
            initialdir=initial if Path(initial).exists() else str(BASE_DIR),
        )
        if selected:
            self.chrome_profile_var.set(selected)

    def chrome_executable_path(self) -> str | None:
        candidates = [
            shutil.which("chrome.exe"),
            shutil.which("chrome"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return str(candidate)
        return None

    def resolved_chrome_profile_path(self) -> Path:
        value = self.chrome_profile_var.get().strip() or "chrome-profile"
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        return path

    def save_chrome_profile_setting(self) -> Path:
        profile_path = self.resolved_chrome_profile_path()
        profile_path.mkdir(parents=True, exist_ok=True)
        raw_config = self.load_raw_config()
        raw_config["chrome_user_data_dir"] = str(profile_path)
        self.save_raw_config(raw_config)
        self.chrome_profile_var.set(str(profile_path))
        return profile_path

    def open_chrome_profile_for_login(self) -> None:
        try:
            chrome_path = self.chrome_executable_path()
            if chrome_path is None:
                raise RuntimeError("Google Chrome was not found on this computer")
            profile_path = self.save_chrome_profile_setting()
            if self.channel_process is not None and self.channel_process.poll() is None:
                self.append_log("INFO | Reusing the already open YouTube channel Chrome window")
                messagebox.showinfo(
                    "YouTube channel",
                    "The Chrome window for this profile is already open.\n\n"
                    "Choose the needed channel there and return here.",
                )
                return
            self.channel_process = subprocess.Popen(
                [
                    chrome_path,
                    f"--user-data-dir={profile_path}",
                    f"--remote-debugging-port={uploader.LOGIN_CHROME_CDP_PORT}",
                    "https://studio.youtube.com",
                ],
                close_fds=True,
            )
            self.append_log(
                "INFO | Opened YouTube channel Chrome profile. Choose the channel, then return to upload."
            )
            messagebox.showinfo(
                "YouTube channel",
                "A normal Chrome window was opened for this profile.\n\n"
                "1. Sign in to YouTube there.\n"
                "2. Choose the needed channel.\n"
                "3. Return here and click Start.\n\n"
                "You can leave this Chrome window open. The uploader will attach to it.",
            )
        except Exception as error:
            messagebox.showerror("Cannot open Chrome profile", str(error))

    def close_channel_process_before_upload(self, show_message: bool = True) -> None:
        if self.channel_process is None or self.channel_process.poll() is not None:
            self.channel_process = None
            return
        if show_message:
            self.append_log("INFO | Leaving the manual Chrome window open for uploader attach")

    def parse_batch_limit(self) -> int:
        value = self.batch_limit_var.get().strip()
        if not value or value.lower() in {"all", "0"}:
            return 0
        try:
            limit = int(value)
        except ValueError as error:
            raise ValueError("Videos must be a number, for example 3, 9, 12, 15, or All") from error
        if limit < 1:
            raise ValueError("Videos must be greater than 0, or use All")
        return limit

    def parse_schedule_times(self) -> list[str]:
        try:
            return uploader.normalize_schedule_daily_times(self.schedule_times_var.get())
        except ValueError as error:
            raise ValueError("Times/day must look like 10:00, 15:00, 20:00") from error

    def default_schedule_start(self, daily_times: list[str]) -> str:
        first_time = daily_times[0] if daily_times else "10:00"
        tomorrow = datetime.now() + timedelta(days=1)
        return f"{tomorrow.strftime('%Y-%m-%d')} {first_time}"

    def automatic_schedule_start(self, daily_times: list[str]) -> str:
        first_time = daily_times[0] if daily_times else "09:00"
        now = datetime.now()
        schedule_start = self.schedule_start_var.get().strip()
        if schedule_start:
            try:
                parsed = uploader.parse_schedule_start(schedule_start)
                if parsed > now:
                    return f"{parsed.strftime('%Y-%m-%d')} {first_time}"
            except Exception:
                pass
        tomorrow = now + timedelta(days=1)
        return f"{tomorrow.strftime('%Y-%m-%d')} {first_time}"

    def sync_schedule_controls(self) -> None:
        unverified = self.is_unverified_channel_preset()
        state = tk.NORMAL if self.manual_schedule_var.get() else tk.DISABLED
        times_state = tk.DISABLED if unverified else state
        if hasattr(self, "schedule_start_entry"):
            self.schedule_start_entry.configure(state=state)
        if hasattr(self, "schedule_times_entry"):
            self.schedule_times_entry.configure(state=times_state)
        if hasattr(self, "batch_limit_entry"):
            self.batch_limit_entry.configure(state=tk.DISABLED if unverified else tk.NORMAL)
        if hasattr(self, "morning_plan_button"):
            self.morning_plan_button.configure(state=tk.DISABLED if unverified else tk.NORMAL)

    def on_manual_schedule_changed(self) -> None:
        self.sync_schedule_controls()
        self.preview_queue(show_message=False)

    def on_channel_preset_changed(self) -> None:
        key = self.channel_preset_key()
        self.channel_preset_hint_var.set(CHANNEL_PRESETS[key]["hint"])
        if key in UNVERIFIED_CHANNEL_PRESETS:
            preset = CHANNEL_PRESETS[key]
            self.batch_limit_var.set(str(preset.get("max_videos", STANDARD_VIDEOS_PER_DAY)))
            self.schedule_times_var.set("1 раз в день")
            if not self.manual_schedule_var.get():
                self.schedule_start_var.set(self.one_video_daily_start(str(preset.get("start_time", "09:00"))))
        elif self.schedule_times_var.get().strip() == "1 раз в день":
            self.batch_limit_var.set(str(STANDARD_BATCH_LIMIT))
            self.schedule_times_var.set(STANDARD_CHANNEL_DAILY_TIMES)
        self.sync_schedule_controls()
        self.preview_queue(show_message=False)

    def current_auto_slot_offset(self, raw_config: dict) -> int:
        slot_key = str(raw_config.get("schedule_language_slot") or "manual").strip().lower()
        if slot_key != "manual" and slot_key in uploader.SCHEDULE_SLOT_OFFSETS:
            return uploader.SCHEDULE_SLOT_OFFSETS[slot_key]
        try:
            return max(0, int(raw_config.get("schedule_slot_offset_minutes") or 0))
        except (TypeError, ValueError):
            return 0

    def update_auto_slot_hint(self, raw_config: dict | None = None) -> None:
        if raw_config is None:
            try:
                raw_config = self.load_raw_config()
            except Exception:
                raw_config = {}
        offset = self.current_auto_slot_offset(raw_config)
        self.auto_slot_hint_var.set(f"Next batch starts at base +{offset} min; then +15 min per 3 videos")

    def use_morning_plan(self) -> None:
        self.manual_schedule_var.set(False)
        self.sync_schedule_controls()
        self.schedule_times_var.set("09:00, 14:00, 19:00")
        self.reset_auto_slot_offset = True
        schedule_start = self.schedule_start_var.get().strip()
        if schedule_start:
            try:
                parsed = uploader.parse_schedule_start(schedule_start)
                self.schedule_start_var.set(f"{parsed.strftime('%Y-%m-%d')} 09:00")
            except Exception:
                pass
        self.preview_queue(show_message=False)

    def apply_settings(self, show_errors: bool = True) -> bool:
        try:
            source_folder = self.source_folder_from_entry()
            if not source_folder.exists() or not source_folder.is_dir():
                raise ValueError(f"Folder does not exist: {source_folder}")
            source_folder = source_folder.resolve()
            self.source_folder_var.set(str(source_folder))

            raw_config = self.load_raw_config()
            raw_config["video_folder"] = str(source_folder)
            raw_config["chrome_user_data_dir"] = self.chrome_profile_var.get().strip() or "chrome-profile"
            raw_config["schedule_enabled"] = True
            raw_config["stop_before_publish"] = True
            raw_config["channel_switch_enabled"] = False
            raw_config["channel_switch_match_texts"] = []
            channel_preset = self.channel_preset_key()
            raw_config["channel_preset"] = channel_preset
            existing_active_channel = str(raw_config.get("active_channel") or "").strip()
            inferred_channel = uploader.detect_channel_key_from_folder(source_folder)
            if inferred_channel in UNVERIFIED_CHANNEL_PRESETS and channel_preset != inferred_channel:
                channel_preset = inferred_channel
                raw_config["channel_preset"] = channel_preset
                self.set_channel_preset_key(channel_preset)
            if channel_preset != STANDARD_CHANNEL_PRESET:
                raw_config["active_channel"] = channel_preset
            elif inferred_channel is not None:
                raw_config["active_channel"] = inferred_channel
            elif existing_active_channel and existing_active_channel != "default":
                # Keep the last explicit channel only when the source folder name is not informative.
                raw_config["active_channel"] = existing_active_channel
            else:
                raw_config["active_channel"] = "default"
            slot_offset = self.current_auto_slot_offset(raw_config)
            if self.reset_auto_slot_offset:
                slot_offset = 0
                self.reset_auto_slot_offset = False

            if channel_preset in UNVERIFIED_CHANNEL_PRESETS:
                preset = CHANNEL_PRESETS[channel_preset]
                max_videos = int(preset.get("max_videos", STANDARD_VIDEOS_PER_DAY))
                raw_config["max_videos_per_run"] = max_videos
                raw_config["limit_scope"] = "run"
                raw_config["manual_schedule_enabled"] = bool(self.manual_schedule_var.get())
                raw_config["schedule_interval_hours"] = 24
                raw_config["schedule_language_slot"] = "manual"
                raw_config["schedule_slot_offset_minutes"] = 0
                schedule_times = []
                self.batch_limit_var.set(str(max_videos))
                self.schedule_times_var.set("1 раз в день")
                if self.manual_schedule_var.get():
                    schedule_start = self.schedule_start_var.get().strip()
                else:
                    schedule_start = self.one_video_daily_start(str(preset.get("start_time", "09:00")))
                    self.schedule_start_var.set(schedule_start)
            else:
                raw_config["max_videos_per_run"] = self.parse_batch_limit()
                raw_config["limit_scope"] = "run"
                raw_config["manual_schedule_enabled"] = bool(self.manual_schedule_var.get())
                raw_config["schedule_language_slot"] = "manual"
                raw_config["schedule_interval_hours"] = int(raw_config.get("schedule_interval_hours") or 8)

                if self.manual_schedule_var.get():
                    raw_config["schedule_slot_offset_minutes"] = 0
                    schedule_times = self.parse_schedule_times()
                    schedule_start = self.schedule_start_var.get().strip()
                else:
                    raw_config["schedule_slot_offset_minutes"] = slot_offset
                    schedule_times = [item.strip() for item in STANDARD_CHANNEL_DAILY_TIMES.split(",")]
                    schedule_start = self.automatic_schedule_start(schedule_times)
                    self.schedule_times_var.set(", ".join(schedule_times))
                    self.schedule_start_var.set(schedule_start)

            if self.schedule_enabled_var.get():
                if not schedule_start:
                    if channel_preset in UNVERIFIED_CHANNEL_PRESETS:
                        schedule_start = self.one_video_daily_start(
                            str(CHANNEL_PRESETS[channel_preset].get("start_time", "09:00"))
                        )
                    else:
                        schedule_start = self.default_schedule_start(schedule_times)
                    self.schedule_start_var.set(schedule_start)
                uploader.parse_schedule_start(schedule_start)

            raw_config["schedule_start_datetime"] = schedule_start
            raw_config["schedule_daily_times"] = schedule_times
            active_channel = str(raw_config.get("active_channel") or "default").strip() or "default"
            channel_schedules = raw_config.get("channel_schedules")
            if not isinstance(channel_schedules, dict):
                channel_schedules = {}
            # Keep the active channel schedule aligned with the form, otherwise
            # a stale passport entry can override the current manual date/time.
            channel_schedules[active_channel] = {
                "schedule_start_datetime": schedule_start,
                "schedule_daily_times": schedule_times,
                "schedule_interval_hours": int(raw_config.get("schedule_interval_hours") or 8),
                "schedule_language_slot": str(raw_config.get("schedule_language_slot") or "manual"),
                "schedule_slot_offset_minutes": int(raw_config.get("schedule_slot_offset_minutes") or 0),
                "max_videos_per_run": int(raw_config.get("max_videos_per_run") or 0),
            }
            raw_config["channel_schedules"] = channel_schedules
            self.save_raw_config(raw_config)
            self.update_auto_slot_hint(raw_config)
            return True
        except Exception as error:
            if show_errors:
                messagebox.showerror("Cannot use these settings", str(error))
            else:
                self.append_log(f"ERROR | {error}")
            return False

    def preview_queue(self, show_message: bool = True) -> None:
        if not self.apply_settings(show_errors=show_message):
            return
        self.video_tree.delete(*self.video_tree.get_children())
        try:
            config = uploader.load_config(CONFIG_PATH)
            uploader.ensure_directories(config)
            pairs = uploader.find_video_pairs(config.video_folder, ScanLogger(self))
            all_pairs_count = len(pairs)
            queued_pairs = uploader.limit_video_pairs(pairs, config)
            queued_paths = {pair.video_path for pair in queued_pairs}
            schedule_start = uploader.parse_schedule_start(config.schedule_start_datetime) if config.schedule_enabled else None
            limit_block_label = "\u0421\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0439 \u0437\u0430\u043f\u0443\u0441\u043a"
            safety_label = " \u043d\u0430 \u0437\u0430\u043f\u0443\u0441\u043a"
            progress_limit_label = "\u043f\u0430\u0447\u043a\u0430 \u0442\u0435\u043a\u0443\u0449\u0435\u0433\u043e \u0437\u0430\u043f\u0443\u0441\u043a\u0430 \u043f\u0443\u0441\u0442\u0430"

            for index, pair in enumerate(pairs):
                try:
                    metadata = uploader.parse_metadata(pair.metadata_path)
                    title = metadata.title
                    status = "Готово" if pair.video_path in queued_paths else limit_block_label
                except Exception as error:
                    title = str(error)
                    status = "Ошибка"

                schedule_label = ""
                if schedule_start is not None and pair.video_path in queued_paths:
                    queued_index = queued_pairs.index(pair)
                    schedule_time = uploader.schedule_time_for_index(config, queued_index, schedule_start)
                    schedule_label = schedule_time.strftime("%Y-%m-%d %H:%M")

                tag = "error" if status == "Ошибка" else ("odd" if index % 2 else "even")
                self.video_tree.insert(
                    "",
                    tk.END,
                    values=(f"{index + 1:03d}", pair.video_path.name, title, schedule_label, status),
                    tags=(tag,),
                )

            limit_label = config.max_videos_per_run or "All"
            self.summary_var.set(
                f"К загрузке {len(queued_pairs)} из {all_pairs_count} • Лимит {limit_label}{safety_label}"
            )
            self.status_var.set(f"Готово: {len(queued_pairs)} к загрузке / {all_pairs_count} найдено")
            self.source_count_var.set(f"{all_pairs_count} видео найдено")
            if all_pairs_count == 0:
                self.progress_var.set("Готово: в папке нет видео для загрузки")
            elif len(queued_pairs) == 0:
                self.progress_var.set(
                    f"Готово: найдено {all_pairs_count} видео, но {progress_limit_label}"
                )
            else:
                self.progress_var.set(
                    f"Готово: к загрузке {len(queued_pairs)} из {all_pairs_count} видео"
                )
            if show_message:
                self.append_log(f"INFO | Queue preview: {len(queued_pairs)} queued / {all_pairs_count} found")
        except Exception as error:
            self.status_var.set("Ошибка настроек")
            self.append_log(f"ERROR | {error}")
            messagebox.showerror("Cannot preview queue", str(error))

    def start_upload(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        if not self.apply_settings():
            return
        self.preview_queue(show_message=False)

        try:
            config = uploader.load_config(CONFIG_PATH)
            pairs = uploader.find_video_pairs(config.video_folder, ScanLogger(self))
            self.run_total_videos = len(uploader.limit_video_pairs(pairs, config))
        except Exception:
            self.run_total_videos = 0

        self.stop_event.clear()
        self.uploaded_this_run = 0
        self.worker_had_error = False
        self.set_running_state(True)
        self.status_var.set("Upload running")
        self.progress_var.set(f"Загрузка началась: загружено 0 из {self.run_total_videos} видео")
        self.append_log("INFO | Starting upload in Auto Save/Schedule mode")

        self.worker_thread = threading.Thread(target=self.worker_main, daemon=True)
        self.worker_thread.start()

    def worker_main(self) -> None:
        try:
            config = uploader.load_config(CONFIG_PATH)
            uploader.run_uploads(
                config,
                dry_run=False,
                confirmation_callback=self.confirmation_callback,
                uploaded_callback=self.enqueue_uploaded,
                stop_event=self.stop_event,
                log_callback=self.enqueue_log,
            )
        except Exception as error:
            self.enqueue_log(f"ERROR | {error}")
            self.ui_queue.put(("worker_error", str(error)))
        finally:
            self.ui_queue.put(("worker_done", None))

    def confirmation_callback(
        self,
        pair: uploader.VideoPair,
        metadata: uploader.Metadata,
        config: uploader.Config,
        schedule_time: object,
    ) -> str:
        self.append_log(
            "WARNING | Manual confirmation was requested unexpectedly; continuing with auto mode"
        )
        return ""

    def enqueue_uploaded(self, report_row: dict[str, str]) -> None:
        self.ui_queue.put(("uploaded", report_row))

    def add_uploaded_row(self, report_row: dict[str, str], announce: bool = True) -> None:
        source_video = report_row.get("source_video", "")
        if announce:
            self.uploaded_this_run += 1
            total = self.run_total_videos or self.uploaded_this_run
            self.progress_var.set(f"Загружено {self.uploaded_this_run} из {total} видео")
            self.append_log(f"INFO | Uploaded and archived: {source_video}")

    def advance_auto_slot_offset_after_run(self) -> None:
        if self.uploaded_this_run <= 0:
            return
        try:
            raw_config = self.load_raw_config()
            if raw_config.get("manual_schedule_enabled", False):
                self.uploaded_this_run = 0
                return
            if str(raw_config.get("channel_preset") or "") in UNVERIFIED_CHANNEL_PRESETS:
                self.uploaded_this_run = 0
                return
            daily_times = uploader.normalize_schedule_daily_times(
                raw_config.get("schedule_daily_times") or self.schedule_times_var.get()
            )
            slots_per_group = max(1, len(daily_times))
            groups_used = (self.uploaded_this_run + slots_per_group - 1) // slots_per_group
            current_offset = self.current_auto_slot_offset(raw_config)
            next_offset = current_offset + groups_used * uploader.SCHEDULE_GROUP_STEP_MINUTES
            raw_config["schedule_language_slot"] = "manual"
            raw_config["schedule_slot_offset_minutes"] = next_offset
            self.save_raw_config(raw_config)
            self.update_auto_slot_hint(raw_config)
            self.append_log(
                f"INFO | Auto slots advanced by {groups_used * uploader.SCHEDULE_GROUP_STEP_MINUTES} min; "
                f"next batch starts at base +{next_offset} min"
            )
        except Exception as error:
            self.append_log(f"WARNING | Could not advance auto slots: {error}")
        finally:
            self.uploaded_this_run = 0

    def stop_after_current(self) -> None:
        self.stop_event.set()
        self.status_var.set("Stop requested")
        total = self.run_total_videos or self.uploaded_this_run
        self.progress_var.set(f"Остановка: загружено {self.uploaded_this_run} из {total} видео")
        self.append_log("INFO | Stop requested; no new video will be started")

    def play_channel_complete_signal(self, success: bool) -> None:
        def play() -> None:
            if winsound is not None:
                tones = ((1046, 180), (1318, 260)) if success else ((523, 180), (392, 260))
                try:
                    for frequency, duration in tones:
                        winsound.Beep(frequency, duration)
                    return
                except RuntimeError:
                    pass
            try:
                self.root.after(0, self.root.bell)
            except Exception:
                pass

        threading.Thread(target=play, daemon=True).start()

    def set_running_state(self, running: bool) -> None:
        normal = tk.NORMAL
        disabled = tk.DISABLED
        self.start_button.configure(state=disabled if running else normal)
        self.stop_button.configure(state=normal if running else disabled)

    def enqueue_log(self, message: str) -> None:
        self.ui_queue.put(("log", message))

    def append_log(self, message: str) -> None:
        if not hasattr(self, "log_text"):
            return
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, message.rstrip() + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def poll_ui_queue(self) -> None:
        try:
            while True:
                event, payload = self.ui_queue.get_nowait()
                if event == "log":
                    self.append_log(str(payload))
                elif event == "uploaded":
                    self.add_uploaded_row(payload)
                elif event == "worker_error":
                    self.worker_had_error = True
                    total = self.run_total_videos or self.uploaded_this_run
                    self.progress_var.set(f"Остановлено: загружено {self.uploaded_this_run} из {total} видео")
                    messagebox.showerror("Upload stopped", str(payload))
                elif event == "worker_done":
                    self.set_running_state(False)
                    self.status_var.set("Ready")
                    total = self.run_total_videos or self.uploaded_this_run
                    interrupted = self.worker_had_error or self.stop_event.is_set()
                    should_signal = bool(total or self.uploaded_this_run)
                    if not self.worker_had_error:
                        final_progress = f"Готово: загружено {self.uploaded_this_run} из {total} видео"
                    else:
                        final_progress = f"Остановлено: загружено {self.uploaded_this_run} из {total} видео"
                    self.advance_auto_slot_offset_after_run()
                    self.preview_queue(show_message=False)
                    self.progress_var.set(final_progress)
                    if should_signal:
                        self.play_channel_complete_signal(success=not interrupted)
        except queue.Empty:
            pass
        self.root.after(100, self.poll_ui_queue)

    def on_close(self) -> None:
        self.stop_event.set()
        self.channel_process = None
        self.root.destroy()


def configure_windows_taskbar_icon() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        logging.getLogger(__name__).debug("Could not set Windows AppUserModelID", exc_info=True)


def main() -> None:
    configure_windows_taskbar_icon()
    root = tk.Tk()
    app = UploaderGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
