"""Korean desktop interface for collecting original travel evidence screens."""

from __future__ import annotations

import os
import queue
import re
import time
import tkinter as tk
import webbrowser
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__
from .automation import AutomationWorker
from .credentials import WindowsCredentialStore
from .macro_dialog import BrowserMacroDialog
from .workflow_dialog import WorkflowDialog
from .models import FUEL_LABELS, SITE_LABELS, TRANSPORT_LABELS, TRANSPORT_SITES, RunRequest
from .native_capture import NativeCaptureDialog
from .platformpaths import load_settings, save_settings


class TripCaptureApp:
    """Own the Tk event loop; browser work is delegated to a worker thread."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"출장 증빙 캡처 v{__version__}")
        self.root.geometry("1000x830")
        self.root.minsize(900, 740)
        self.root.configure(background="#f1f5f9")
        self.events: queue.Queue = queue.Queue()
        self.credentials = WindowsCredentialStore()
        self.worker = AutomationWorker(self.events)
        self.run_state = "idle"
        self.current_site = ""
        self.last_report: Path | None = None
        self.last_output: Path | None = None
        self._closed = False
        self._close_deadline = 0.0
        self._settings_error = ""
        try:
            self.settings = load_settings()
        except Exception as exc:
            self.settings = {}
            self._settings_error = f"이전 설정을 불러오지 못했습니다: {exc}"
        if not isinstance(self.settings, dict):
            self.settings = {}

        self.trip_date = tk.StringVar(value=date.today().isoformat())
        self.origin = tk.StringVar(value=str(self.settings.get("origin", "")))
        self.destination = tk.StringVar(value=str(self.settings.get("destination", "")))
        saved_fuel = self.settings.get("fuel", "diesel")
        self.fuel = tk.StringVar(value=FUEL_LABELS.get(saved_fuel, FUEL_LABELS["diesel"]))
        self.browser = tk.StringVar(value="Edge" if self.settings.get("browser") == "msedge" else "Chrome")
        self.output_dir = tk.StringVar(
            value=str(self.settings.get("output_dir", Path.home() / "Documents" / "출장증빙"))
        )
        self.hipass_id = tk.StringVar(value=str(self.settings.get("hipass_id", "")))
        self.hipass_password = tk.StringVar()
        self.trip_details = {
            name: tk.StringVar(value=str(self.settings.get(name, "")))
            for name in ("trip_end_date", "department", "position", "traveler_name", "trip_purpose", "rail_origin", "rail_destination", "rail_return_origin", "rail_return_destination")
        }
        for name, variable in self.trip_details.items():
            setattr(self, name, variable)
        if self.settings.get("trip_date") != self.trip_date.get():
            self.trip_end_date.set("")
        saved_sites = self.settings.get("sites")
        saved_sites = [site for site in saved_sites if isinstance(site, str) and site in SITE_LABELS] if isinstance(saved_sites, (list, tuple)) else None
        saved_mode = self.settings.get("transport_mode")
        if not isinstance(saved_mode, str):
            saved_mode = ""
        mode = saved_mode if saved_mode in TRANSPORT_LABELS else ("train" if saved_sites and "korail" in saved_sites else "car")
        self.transport_mode = tk.StringVar(value=mode)
        self._visible_mode = mode
        self._mode_sites = {key: tuple(values) for key, values in TRANSPORT_SITES.items()}
        preferences = self.settings.get("mode_sites", {})
        if isinstance(preferences, dict):
            for key, allowed in TRANSPORT_SITES.items():
                selected = preferences.get(key)
                if isinstance(selected, (list, tuple)):
                    self._mode_sites[key] = tuple(site for site in allowed if site in selected)
        if saved_sites is not None:
            self._mode_sites[mode] = tuple(site for site in TRANSPORT_SITES[mode] if site in saved_sites)
            legacy_car = tuple(site for site in TRANSPORT_SITES["car"] if site in saved_sites)
            if saved_mode not in TRANSPORT_LABELS and legacy_car:
                self._mode_sites["car"] = legacy_car
        if mode == "train":
            self._mode_sites["train"] = ("korail",)
        selected_sites = set(self._mode_sites[mode])
        self.site_vars = {key: tk.BooleanVar(value=key in selected_sites) for key in SITE_LABELS}
        self._requested_sites = tuple(site for site in SITE_LABELS if site in selected_sites)
        self._step_index = 0
        self._step_total = len(self._requested_sites)
        self._step_site = ""
        self._next_site = ""
        self.page_selection = tk.StringVar()
        self._page_ids: dict[str, str] = {}
        self._manual_site = ""
        self._browserless = False
        self._korail_browser_opened = False
        self._receipt_count = 0
        self._native_dialog: NativeCaptureDialog | None = None
        self._macro_dialog: BrowserMacroDialog | WorkflowDialog | None = None
        self._closing_macro: BrowserMacroDialog | WorkflowDialog | None = None
        self._macro_busy = False
        self.status = tk.StringVar(value="출장 정보를 입력한 후 캡처를 시작하세요.")
        self.manual_instruction = tk.StringVar(value="사이트에서 화면을 준비해야 할 때 여기에 안내가 표시됩니다.")
        self.credential_status = tk.StringVar(value="저장한 로그인 정보가 없어도 직접 로그인할 수 있습니다.")
        self.input_controls: list[ttk.Widget] = []
        self._build_styles()
        self._build_ui()
        self._apply_mode_visibility()
        self._update_progress_title()
        self._refresh_credential_status()
        if self._settings_error:
            self._log(self._settings_error)
        self._log("원본 화면을 PNG로 저장하고, 출처와 캡처 시각을 담은 HTML 보고서를 만듭니다.")
        self.worker.start()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(120, self._poll_events)

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background="#f1f5f9")
        style.configure("TLabel", background="#f1f5f9", foreground="#25344a", font=("맑은 고딕", 10))
        style.configure("TLabelframe", background="#f1f5f9", bordercolor="#cbd5e1")
        style.configure("TLabelframe.Label", background="#f1f5f9", foreground="#1e293b", font=("맑은 고딕", 10, "bold"))
        style.configure("TCheckbutton", background="#f1f5f9", font=("맑은 고딕", 10))
        style.configure("TEntry", padding=5, font=("맑은 고딕", 10))
        style.configure("TCombobox", padding=4, font=("맑은 고딕", 10))
        style.configure("TButton", padding=(11, 6), font=("맑은 고딕", 9))
        style.configure("Accent.TButton", background="#1d4ed8", foreground="white", font=("맑은 고딕", 10, "bold"))
        style.map("Accent.TButton", background=[("disabled", "#94a3b8"), ("active", "#1e40af")])
        style.configure("Hint.TLabel", foreground="#64748b", font=("맑은 고딕", 9))
        style.configure("Status.TLabel", foreground="#1d4ed8", font=("맑은 고딕", 11, "bold"))

    def _build_ui(self) -> None:
        body = ttk.Frame(self.root, padding=(20, 10))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(5, weight=1)

        header = ttk.Frame(body)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        title_row = ttk.Frame(header)
        title_row.pack(fill="x")
        ttk.Label(title_row, text="출장 증빙 캡처", font=("맑은 고딕", 21, "bold"), foreground="#0f284b").pack(side="left")
        self.macro_button = ttk.Button(title_row, text="브라우저 버튼 매크로", command=self._open_browser_macro)
        self.macro_button.pack(side="right")
        self.workflow_button = ttk.Button(title_row, text="로그인부터 전체 자동화", command=self._open_workflow)
        self.workflow_button.pack(side="right", padx=(0, 8))
        modes = ttk.Frame(header)
        modes.pack(anchor="w", pady=(5, 0))
        self.mode_buttons = []
        for code, label in TRANSPORT_LABELS.items():
            button = ttk.Radiobutton(modes, text=label, value=code, variable=self.transport_mode, command=self._mode_changed)
            button.pack(side="left", padx=(0, 25))
            self.mode_buttons.append(button)
            self.input_controls.append(button)
        ttk.Label(modes, text="유형별 증빙을 따로 수집합니다.", style="Hint.TLabel").pack(side="left")

        info_container = ttk.LabelFrame(body, text="출장 정보", padding=(8, 5))
        info_container.grid(row=1, column=0, sticky="ew", pady=(0, 9))
        self.details_notebook = ttk.Notebook(info_container)
        self.details_notebook.pack(fill="both", expand=True)
        info = ttk.Frame(self.details_notebook, padding=(4, 3))
        self.details_notebook.add(info, text="기본 정보")
        self.common_tab = ttk.Frame(self.details_notebook, padding=(8, 6))
        self.details_notebook.add(self.common_tab, text="공통 출장정보")
        self.korail_tab = ttk.Frame(self.details_notebook, padding=(8, 6))
        self.details_notebook.add(self.korail_tab, text="철도 구간 · 코레일")
        info.columnconfigure(1, weight=1)
        info.columnconfigure(3, weight=1)
        info.columnconfigure(5, weight=1)
        ttk.Label(info, text="출장 시작일").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self._entry(info, self.trip_date, width=14).grid(row=0, column=1, sticky="ew", padx=(0, 15))
        ttk.Label(info, text="종료일(선택)").grid(row=0, column=2, sticky="w", padx=(0, 8))
        self._entry(info, self.trip_end_date, width=14).grid(row=0, column=3, sticky="ew", padx=(0, 15))
        browser_label = ttk.Label(info, text="브라우저")
        browser_label.grid(row=0, column=4, sticky="w", padx=(0, 8))
        browser_combo = ttk.Combobox(info, textvariable=self.browser, values=("Chrome", "Edge"), state="readonly", width=12)
        browser_combo.grid(row=0, column=5, sticky="ew")
        self.input_controls.append(browser_combo)
        self.browser_widgets = (browser_label, browser_combo)
        ttk.Label(info, text="출발지").grid(row=1, column=0, sticky="w", pady=(8, 0), padx=(0, 8))
        self._entry(info, self.origin).grid(row=1, column=1, columnspan=2, sticky="ew", pady=(8, 0), padx=(0, 15))
        ttk.Label(info, text="출장지").grid(row=1, column=3, sticky="w", pady=(8, 0))
        self._entry(info, self.destination).grid(row=1, column=4, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(info, text="저장 위치").grid(row=2, column=0, sticky="w", pady=(8, 0), padx=(0, 8))
        self._entry(info, self.output_dir).grid(row=2, column=1, columnspan=4, sticky="ew", pady=(8, 0), padx=(0, 8))
        browse = ttk.Button(info, text="폴더 선택", command=self._choose_folder)
        browse.grid(row=2, column=5, sticky="ew", pady=(8, 0))
        self.input_controls.append(browse)
        ttk.Label(info, text="캡처 대상").grid(row=3, column=0, sticky="w", pady=(8, 0))
        sites = ttk.Frame(info)
        sites.grid(row=3, column=1, columnspan=3, sticky="w", pady=(8, 0))
        self.site_controls = {}
        for key, label in SITE_LABELS.items():
            check = ttk.Checkbutton(sites, text=label, variable=self.site_vars[key],
                                    command=lambda selected_site=key: self._sites_changed(selected_site))
            check.pack(side="left", padx=(0, 14))
            self.site_controls[key] = check
            self.input_controls.append(check)
        fuel_label = ttk.Label(info, text="유종")
        fuel_label.grid(row=3, column=4, sticky="w", padx=(0, 8), pady=(8, 0))
        self.fuel_combo = ttk.Combobox(info, textvariable=self.fuel, values=list(FUEL_LABELS.values()), state="readonly", width=15)
        self.fuel_combo.grid(row=3, column=5, sticky="ew", pady=(8, 0))
        self.input_controls.append(self.fuel_combo)
        self.car_fuel_widgets = (fuel_label, self.fuel_combo)
        self._build_common_fields()
        self._build_korail_fields()

        login = ttk.LabelFrame(body, text="하이패스 로그인 정보 · 선택 사항 · Windows 자격 증명에 저장", padding=(12, 9))
        self.hipass_login_frame = login
        login.grid(row=2, column=0, sticky="ew", pady=(0, 9))
        login.columnconfigure(1, weight=1)
        login.columnconfigure(3, weight=1)
        ttk.Label(login, text="아이디").grid(row=0, column=0, sticky="w", padx=(0, 8))
        id_entry = self._entry(login, self.hipass_id, width=18)
        id_entry.grid(row=0, column=1, sticky="ew", padx=(0, 16))
        id_entry.bind("<FocusOut>", lambda _event: self._refresh_credential_status())
        ttk.Label(login, text="비밀번호").grid(row=0, column=2, sticky="w", padx=(0, 8))
        self._entry(login, self.hipass_password, show="●", width=18).grid(row=0, column=3, sticky="ew", padx=(0, 8))
        save_login = ttk.Button(login, text="이 PC에 로그인 정보 저장", command=self._save_credentials)
        save_login.grid(row=0, column=4, padx=(0, 6))
        delete_login = ttk.Button(login, text="저장 정보 삭제", command=self._delete_credentials)
        delete_login.grid(row=0, column=5)
        self.input_controls.extend((save_login, delete_login))
        ttk.Label(login, textvariable=self.credential_status, style="Hint.TLabel").grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))

        progress = ttk.LabelFrame(body, text="진행 상태", padding=(12, 9))
        self.progress_frame = progress
        progress.grid(row=3, column=0, sticky="ew", pady=(0, 9))
        progress.columnconfigure(0, weight=1)
        ttk.Label(progress, textvariable=self.status, style="Status.TLabel", wraplength=890).grid(row=0, column=0, sticky="w")
        ttk.Label(progress, textvariable=self.manual_instruction, wraplength=890).grid(row=1, column=0, sticky="w", pady=(6, 0))
        page_controls = ttk.Frame(progress)
        self.page_controls = page_controls
        page_controls.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        page_controls.columnconfigure(1, weight=1)
        ttk.Label(page_controls, text="저장할 창").grid(row=0, column=0, padx=(0, 8))
        self.page_combo = ttk.Combobox(page_controls, textvariable=self.page_selection, state="disabled", width=65)
        self.page_combo.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.page_combo.bind("<<ComboboxSelected>>", lambda _event: self._sync_page_controls())
        self.refresh_button = ttk.Button(page_controls, text="창 목록 새로고침", command=self._refresh_pages, state="disabled")
        self.refresh_button.grid(row=0, column=2)
        actions = ttk.Frame(progress)
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.start_button = ttk.Button(actions, text="캡처 시작", style="Accent.TButton", command=self._start)
        self.start_button.pack(side="left", padx=(0, 8))
        self.capture_button = ttk.Button(actions, text="현재 화면 저장", command=self._capture, state="disabled")
        self.capture_button.pack(side="left", padx=(0, 8))
        self.skip_button = ttk.Button(actions, text="이 사이트 건너뛰기", command=self._skip, state="disabled")
        self.skip_button.pack(side="left", padx=(0, 8))
        self.finish_button = ttk.Button(actions, text="코레일 수집 완료", command=self._finish_receipts, state="disabled")
        self.finish_button.pack(side="left", padx=(0, 8))
        self.korail_retry_button = ttk.Button(actions, text="코레일 열기", command=self._retry_korail, state="disabled")
        self.korail_retry_button.pack(side="left", padx=(0, 8))
        self.cancel_button = ttk.Button(actions, text="중지", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="right")
        receipt_actions = ttk.Frame(progress)
        receipt_actions.grid(row=4, column=0, sticky="ew", pady=(7, 0))
        self.pdf_button = ttk.Button(receipt_actions, text="영수증 PDF 가져오기", command=self._import_pdf, state="disabled")
        self.pdf_button.pack(side="left", padx=(0, 8))
        self.external_button = ttk.Button(receipt_actions, text="일반 브라우저·출력창 저장", command=self._capture_external, state="disabled")
        self.external_button.pack(side="left", padx=(0, 8))
        self.receipt_hint = ttk.Label(receipt_actions, text="직접 저장한 PDF: 원본과 모든 페이지 첨부", style="Hint.TLabel")
        self.receipt_hint.pack(side="left")

        log_frame = ttk.LabelFrame(body, text="작업 기록", padding=7)
        log_frame.grid(row=5, column=0, sticky="nsew", pady=(0, 9))
        self.log_widget = ScrolledText(log_frame, height=3, wrap="word", state="disabled", relief="flat", borderwidth=0, font=("맑은 고딕", 9), background="white", foreground="#334155")
        self.log_widget.pack(fill="both", expand=True)

        results = ttk.Frame(body)
        results.grid(row=6, column=0, sticky="ew")
        ttk.Label(results, text="저장 결과: 원본 PNG 스크린샷 + HTML 보고서", style="Hint.TLabel").pack(side="left")
        self.report_button = ttk.Button(results, text="보고서 열기", command=self._open_report, state="disabled")
        self.report_button.pack(side="right")
        self.folder_button = ttk.Button(results, text="결과 폴더 열기", command=self._open_output, state="disabled")
        self.folder_button.pack(side="right", padx=(0, 8))

    def _entry(self, parent: ttk.Widget, variable: tk.StringVar, **kwargs: object) -> ttk.Entry:
        entry = ttk.Entry(parent, textvariable=variable, **kwargs)
        self.input_controls.append(entry)
        return entry

    def _build_common_fields(self) -> None:
        for column in (1, 3, 5):
            self.common_tab.columnconfigure(column, weight=1)
        self.common_entries = {}
        for label, name, column in (("소속", "department", 0), ("직급", "position", 2), ("성명", "traveler_name", 4)):
            ttk.Label(self.common_tab, text=label).grid(row=0, column=column, sticky="w", padx=(0, 8))
            entry = self._entry(self.common_tab, self.trip_details[name], width=16)
            entry.grid(row=0, column=column + 1, sticky="ew", padx=(0, 12))
            self.common_entries[name] = entry
        ttk.Label(self.common_tab, text="출장 목적").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        purpose = self._entry(self.common_tab, self.trip_purpose)
        purpose.grid(row=1, column=1, columnspan=5, sticky="ew", padx=(0, 12), pady=(10, 0))
        self.common_entries["trip_purpose"] = purpose
        ttk.Label(self.common_tab, text="자차·기차 보고서에 공통으로 사용합니다. 기간은 기본 정보에서 입력하며, 종료일이 빈칸이면 시작일과 같습니다.", style="Hint.TLabel", wraplength=850).grid(row=2, column=0, columnspan=6, sticky="w", pady=(12, 0))

    def _build_korail_fields(self) -> None:
        for column in (1, 3):
            self.korail_tab.columnconfigure(column, weight=1)
        for label, name, row, column in (
            ("가는편 출발역", "rail_origin", 0, 0), ("가는편 도착역", "rail_destination", 0, 2),
            ("귀환 출발역", "rail_return_origin", 1, 0), ("귀환 도착역", "rail_return_destination", 1, 2),
        ):
            ttk.Label(self.korail_tab, text=label).grid(row=row, column=column, sticky="w", padx=(0, 8), pady=(0, 8))
            self._entry(self.korail_tab, self.trip_details[name], width=20).grid(row=row, column=column + 1, sticky="ew", padx=(0, 14), pady=(0, 8))
        ttk.Label(self.korail_tab, text="귀환역 빈칸=가는편 역순. 기본 브라우저에서 직접 로그인 → 이용내역/영수증 조회 → PDF 또는 출력창 추가 → 수집 완료", style="Hint.TLabel", wraplength=560).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.korail_open_button = ttk.Button(self.korail_tab, text="기본 브라우저로 코레일 열기", command=self._open_korail)
        self.korail_open_button.grid(row=2, column=3, sticky="e", padx=(0, 14), pady=(4, 0))

    def _update_korail_details(self, select: bool = False) -> None:
        if self.transport_mode.get() == "train":
            self.details_notebook.add(self.korail_tab, text="철도 구간 · 코레일")
            if select:
                self.details_notebook.select(self.korail_tab)
        else:
            self.details_notebook.hide(self.korail_tab)

    def _apply_mode_visibility(self) -> None:
        train = self.transport_mode.get() == "train"
        self._update_korail_details()
        for site, control in self.site_controls.items():
            control.pack_forget()
            if site in TRANSPORT_SITES[self.transport_mode.get()]:
                control.pack(side="left", padx=(0, 14))
        for widget in (*self.car_fuel_widgets, *self.browser_widgets):
            widget.grid_remove() if train else widget.grid()
        self.hipass_login_frame.grid_remove() if train else self.hipass_login_frame.grid()
        self.page_controls.grid_remove() if train else self.page_controls.grid()
        self.capture_button.pack_forget()
        self.skip_button.pack_forget()
        if not train:
            self.capture_button.pack(side="left", padx=(0, 8), before=self.cancel_button)
            self.skip_button.pack(side="left", padx=(0, 8), before=self.cancel_button)
        self.start_button.configure(text="수집 시작" if train else "캡처 시작")
        self.receipt_hint.configure(text="PDF 원본·모든 페이지 첨부 · 여러 영수증 추가 가능" if train else "직접 저장한 PDF: 원본과 모든 페이지 첨부")
        for button in (self.finish_button, self.korail_retry_button):
            button.pack_forget()
            if train:
                button.pack(side="left", padx=(0, 8), before=self.cancel_button)

    def _mode_changed(self) -> None:
        mode = self.transport_mode.get()
        if self.run_state != "idle" or self._macro_dialog is not None or mode not in TRANSPORT_LABELS:
            self.transport_mode.set(self._visible_mode)
            return
        self._mode_sites[self._visible_mode] = tuple(site for site in TRANSPORT_SITES[self._visible_mode] if self.site_vars[site].get())
        self._visible_mode = mode
        selected = ("korail",) if mode == "train" else self._mode_sites["car"]
        for site, variable in self.site_vars.items():
            variable.set(site in selected)
        self._mode_sites[mode] = tuple(selected)
        self._apply_mode_visibility()
        self._sites_changed("")
        self._refresh_credential_status()
        self.status.set(f"{TRANSPORT_LABELS[mode]} 자료를 수집합니다. 출장 정보를 입력하세요.")
        self.manual_instruction.set("수집 시작 → 기본 브라우저에서 직접 코레일 로그인 → 영수증 PDF 또는 출력창 추가 → 코레일 수집 완료" if mode == "train" else "지도·유가·하이패스 자료를 선택한 순서로 수집합니다.")

    def _sites_changed(self, site: str) -> None:
        if self.run_state != "idle":
            return
        allowed = TRANSPORT_SITES[self.transport_mode.get()]
        for key, variable in self.site_vars.items():
            if key not in allowed:
                variable.set(False)
        self._requested_sites = tuple(key for key in allowed if self.site_vars[key].get())
        self._mode_sites[self.transport_mode.get()] = self._requested_sites
        self._step_index = 0
        self._step_total = len(self._requested_sites)
        self._step_site = ""
        self._next_site = ""
        self._update_progress_title()
        if site == "korail":
            self._update_korail_details(select=True)

    def _open_korail(self) -> bool:
        if self._closed or self._macro_busy:
            return False
        try:
            opened = webbrowser.open("https://www.korail.com/ticket")
        except Exception:
            opened = False
        if not opened:
            self._log("기본 브라우저를 열지 못했습니다. 평소 브라우저에서 https://www.korail.com/ticket 에 직접 접속하세요. PDF·출력창 추가는 계속 사용할 수 있습니다.")
        return bool(opened)

    def _open_browser_macro(self) -> None:
        if self._closed:
            return
        if self._macro_dialog is not None:
            if not self._macro_busy:
                self._macro_dialog.lift()
            return
        if self.run_state == "running":
            return
        if self._native_dialog is not None and self._native_dialog.winfo_exists():
            messagebox.showinfo("브라우저 버튼 매크로", "출력 창 저장을 마치고 매크로를 열어 주세요.", parent=self.root)
            return
        try:
            self._macro_dialog = BrowserMacroDialog(self.root, self._macro_busy_changed, self._macro_closed)
        except Exception as exc:
            self._macro_dialog = None
            messagebox.showerror("매크로 열기", self._safe_message(str(exc)), parent=self.root)
        self._set_run_state(self.run_state)

    def _macro_busy_changed(self, busy: bool) -> None:
        self._macro_busy = bool(busy)
        if not self._closed:
            self._set_run_state(self.run_state)

    def _workflow_request(self) -> RunRequest:
        mode = self.transport_mode.get()
        if mode not in TRANSPORT_SITES:
            raise ValueError("출장 유형을 선택해 주세요.")
        for value in (self.trip_date.get().strip(), self.trip_end_date.get().strip()):
            if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("출장 날짜를 YYYY-MM-DD 형식으로 입력해 주세요.")
        if not self.output_dir.get().strip():
            raise ValueError("증빙을 저장할 폴더를 선택해 주세요.")
        request = RunRequest(
            trip_date=self.trip_date.get().strip(), origin=self.origin.get().strip(),
            destination=self.destination.get().strip(),
            fuel=next((code for code, label in FUEL_LABELS.items() if label == self.fuel.get()), "diesel"),
            sites=tuple(site for site in TRANSPORT_SITES[mode] if self.site_vars[site].get()),
            output_dir=Path(self.output_dir.get().strip()).expanduser(), browser="msedge",
            transport_mode=mode, **{name: variable.get().strip() for name, variable in self.trip_details.items()},
        )
        request.validate()
        return request

    def _open_workflow(self) -> None:
        if self._closed or self.run_state != "idle":
            return
        if self._macro_dialog is not None:
            if not self._macro_busy:
                self._macro_dialog.lift()
            return
        if self._native_dialog is not None and self._native_dialog.winfo_exists():
            return
        try:
            request = self._workflow_request()
            self._macro_dialog = WorkflowDialog(
                self.root, self._macro_busy_changed, self._macro_closed,
                request=request, on_report=self._workflow_report,
            )
        except ValueError as error:
            messagebox.showerror("출장 정보 확인", str(error), parent=self.root)
        except Exception:
            self._macro_dialog = None
            messagebox.showerror("전체 자동화", "전체 자동화 창을 열지 못했습니다. 프로그램을 다시 실행해 주세요.", parent=self.root)
        self._set_run_state(self.run_state)

    def _workflow_report(self, report: Path) -> None:
        if self._closed:
            return
        self.last_report = Path(report)
        self.last_output = self.last_report.parent
        self.status.set("전체 자동화에서 저장한 보고서를 확인할 수 있습니다.")
        self.manual_instruction.set("자동화 창을 닫고 ‘보고서 열기’를 눌러 원본 화면·PDF와 누락 여부를 확인하세요.")
        self._log("전체 자동화의 증빙과 보고서를 저장했습니다.")
        self._set_run_state(self.run_state)

    def _macro_closed(self) -> None:
        self._macro_dialog = None
        self._macro_busy = False
        if not self._closed:
            self._set_run_state(self.run_state)

    def _choose_folder(self) -> None:
        directory = filedialog.askdirectory(parent=self.root, title="출장 증빙을 저장할 폴더", initialdir=self.output_dir.get() or str(Path.home()))
        if directory:
            self.output_dir.set(directory)

    def _credential_account(self) -> str:
        return f"hipass:{self.hipass_id.get().strip()}"

    def _refresh_credential_status(self) -> None:
        if self.transport_mode.get() != "car":
            self.credential_status.set("하이패스 로그인 정보는 자차 이용에서만 사용합니다.")
            return
        if not self.hipass_id.get().strip():
            self.credential_status.set("저장한 로그인 정보가 없어도 직접 로그인할 수 있습니다.")
            return
        try:
            saved = self.credentials.get(self._credential_account())
        except Exception as exc:
            self.credential_status.set("저장 정보를 확인할 수 없습니다. 브라우저에서 직접 로그인할 수 있습니다.")
            self._log(f"로그인 정보 확인: {exc}")
            return
        self.credential_status.set(
            "이 아이디의 비밀번호가 이 PC에 저장되어 있습니다."
            if saved is not None
            else "이 아이디로 저장한 비밀번호가 없습니다. 비워 두면 브라우저에서 직접 로그인합니다."
        )

    def _save_credentials(self) -> None:
        if not self.hipass_id.get().strip() or not self.hipass_password.get():
            messagebox.showinfo("로그인 정보", "하이패스 아이디와 비밀번호를 입력한 후 저장해 주세요.", parent=self.root)
            return
        try:
            self.credentials.set(self._credential_account(), self.hipass_password.get())
            self.hipass_password.set("")
            self._save_settings()
        except Exception as exc:
            messagebox.showerror("저장 실패", f"로그인 정보를 저장하지 못했습니다.\n{self._safe_message(str(exc))}", parent=self.root)
            return
        self._refresh_credential_status()
        self._log("하이패스 로그인 정보를 Windows 자격 증명에 저장했습니다.")

    def _delete_credentials(self) -> None:
        if not self.hipass_id.get().strip():
            messagebox.showinfo("로그인 정보", "저장 정보를 삭제할 아이디를 입력해 주세요.", parent=self.root)
            return
        try:
            self.credentials.delete(self._credential_account())
            self.hipass_password.set("")
        except Exception as exc:
            messagebox.showerror("삭제 실패", f"로그인 정보를 삭제하지 못했습니다.\n{exc}", parent=self.root)
            return
        self._refresh_credential_status()
        self._log("이 아이디의 저장된 하이패스 로그인 정보를 삭제했습니다.")

    def _save_settings(self) -> None:
        # Always construct an allow-list. Never persist password field contents.
        fuel_code = next((code for code, label in FUEL_LABELS.items() if label == self.fuel.get()), "diesel")
        mode = self.transport_mode.get()
        self._mode_sites[mode] = tuple(site for site in TRANSPORT_SITES[mode] if self.site_vars[site].get())
        save_settings({
            "transport_mode": mode,
            "mode_sites": {key: list(values) for key, values in self._mode_sites.items()},
            "trip_date": self.trip_date.get().strip(),
            "origin": self.origin.get().strip(),
            "destination": self.destination.get().strip(),
            "fuel": fuel_code,
            "browser": "msedge" if self.browser.get() == "Edge" else "chrome",
            "output_dir": self.output_dir.get().strip(),
            "hipass_id": self.hipass_id.get().strip(),
            "sites": list(self._mode_sites[mode]),
            **{name: variable.get().strip() for name, variable in self.trip_details.items()},
        })

    def _start(self) -> None:
        if self.run_state != "idle" or self._macro_dialog is not None:
            return
        mode = self.transport_mode.get()
        if mode == "car" and self.hipass_password.get() and self.site_vars["hipass"].get():
            messagebox.showinfo(
                "로그인 정보 저장 필요",
                "입력한 비밀번호를 사용하려면 ‘이 PC에 로그인 정보 저장’을 먼저 눌러 주세요.\n직접 로그인하려면 비밀번호 입력칸을 비운 후 시작하세요.",
                parent=self.root,
            )
            return
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.trip_date.get().strip()):
                raise ValueError("출장일을 YYYY-MM-DD 형식으로 입력해 주세요.")
            if self.trip_end_date.get().strip() and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.trip_end_date.get().strip()):
                raise ValueError("출장 종료일을 YYYY-MM-DD 형식으로 입력하거나 비워 주세요.")
            if not self.output_dir.get().strip():
                raise ValueError("증빙을 저장할 폴더를 선택해 주세요.")
            saved_password = False
            if mode == "car" and self.hipass_id.get().strip() and self.site_vars["hipass"].get():
                try:
                    saved_password = self.credentials.get(self._credential_account()) is not None
                except Exception as exc:
                    self._log(f"저장된 로그인 정보를 사용할 수 없어 직접 로그인합니다: {exc}")
            request = RunRequest(
                trip_date=self.trip_date.get().strip(),
                origin=self.origin.get().strip(),
                destination=self.destination.get().strip(),
                fuel=next(code for code, label in FUEL_LABELS.items() if label == self.fuel.get()),
                sites=tuple(key for key in TRANSPORT_SITES[mode] if self.site_vars[key].get()),
                output_dir=Path(self.output_dir.get().strip()).expanduser(),
                browser="msedge" if self.browser.get() == "Edge" else "chrome",
                hipass_id=self.hipass_id.get().strip() if mode == "car" else "",
                use_saved_password=saved_password,
                transport_mode=mode,
                **{name: variable.get().strip() for name, variable in self.trip_details.items()},
            )
            request.validate()
            self._requested_sites = request.sites
            self._step_index = 0
            self._step_total = len(request.sites)
            self._step_site = ""
            self._next_site = ""
            self._update_progress_title()
            try:
                self._save_settings()
            except Exception as exc:
                self._log(f"설정 저장을 건너뜁니다: {exc}")
            self._set_run_state("running")
            self.current_site = ""
            self._manual_site = ""
            self._browserless = False
            self._korail_browser_opened = False
            self._receipt_count = 0
            self._page_ids.clear()
            self.page_selection.set("")
            self.status.set("코레일 영수증 수집을 준비하고 있습니다." if mode == "train" else "브라우저를 준비하고 있습니다.")
            self.manual_instruction.set("기본 브라우저에서 직접 로그인한 뒤 영수증 PDF 또는 출력창을 추가하세요." if mode == "train" else "자동으로 준비할 수 있는 화면을 먼저 불러옵니다. 안내가 나타나면 브라우저에서 확인하세요.")
            self.worker.submit(request)
        except (ValueError, StopIteration) as exc:
            messagebox.showerror("입력 확인", str(exc) or "유종을 선택해 주세요.", parent=self.root)
        except Exception as exc:
            self._set_run_state("idle")
            self.status.set("작업을 시작하지 못했습니다.")
            messagebox.showerror("시작 실패", self._safe_message(str(exc)), parent=self.root)

    def _capture(self) -> None:
        if self.run_state == "waiting" and self._macro_dialog is None:
            page_id = self._page_ids.get(self.page_selection.get())
            if not page_id:
                self.manual_instruction.set("‘창 목록 새로고침’을 누른 후 저장할 브라우저 창을 선택해 주세요.")
                self._refresh_pages()
                return
            self._set_run_state("running")
            self.status.set("현재 화면을 저장하고 있습니다.")
            self.worker.capture_current(page_id)

    def _refresh_pages(self) -> None:
        if self.run_state == "waiting" and self._macro_dialog is None:
            self.worker.refresh_pages()

    def _capture_external(self) -> None:
        if self.run_state != "waiting" or self.current_site not in ("hipass", "korail") or self._closed or self._macro_dialog is not None:
            return
        if self._native_dialog is not None and self._native_dialog.winfo_exists():
            self._native_dialog.lift()
            return
        self._native_dialog = NativeCaptureDialog(self.root, self._external_captured, site=self.current_site)

    def _external_captured(self, path: Path, title: str) -> None:
        if self._closed or self.run_state != "waiting" or self.current_site not in ("hipass", "korail") or self._macro_dialog is not None:
            path.unlink(missing_ok=True)
            return
        self._set_run_state("running")
        self.status.set("선택한 영수증·출력 창의 화면을 보고서에 넣고 있습니다.")
        try:
            self.worker.capture_external(path, title)
        except Exception:
            path.unlink(missing_ok=True)
            self._set_run_state("waiting")
            self.status.set("저장한 영수증 화면을 전달하지 못했습니다.")
            self.manual_instruction.set("‘일반 브라우저·출력창 저장’을 다시 눌러 주세요.")

    def _import_pdf(self) -> None:
        if self.run_state != "waiting" or self.current_site not in ("hipass", "korail") or self._closed or self._macro_dialog is not None:
            return
        selected = filedialog.askopenfilename(
            parent=self.root, title=f"저장한 {SITE_LABELS.get(self.current_site, '')} PDF 선택",
            filetypes=[("PDF 영수증", "*.pdf")],
        )
        if not selected:
            return
        self._set_run_state("running")
        self.status.set("영수증 PDF 원본과 모든 페이지를 보고서에 저장하고 있습니다.")
        try:
            self.worker.import_pdf(Path(selected))
        except Exception:
            self._set_run_state("waiting")
            self.status.set("영수증 PDF를 전달하지 못했습니다. 파일을 다시 선택해 주세요.")

    def _sync_page_controls(self) -> None:
        waiting = self.run_state == "waiting" and not self._closed and self._macro_dialog is None
        selected = self._page_ids.get(self.page_selection.get())
        managed = waiting and not self._browserless
        self.capture_button.state(["!disabled"] if managed and selected else ["disabled"])
        self.refresh_button.state(["!disabled"] if managed else ["disabled"])
        self.page_combo.state(["!disabled", "readonly"] if managed and self._page_ids else ["disabled"])
        receipts = waiting and self.current_site in ("hipass", "korail")
        self.external_button.state(["!disabled"] if receipts else ["disabled"])
        self.pdf_button.state(["!disabled"] if receipts else ["disabled"])
        can_finish = waiting and self.current_site == "korail" and (not self._browserless or self._receipt_count > 0)
        self.finish_button.state(["!disabled"] if can_finish else ["disabled"])
        self.korail_retry_button.state(["!disabled"] if waiting and self.current_site == "korail" else ["disabled"])

    def _finish_receipts(self) -> None:
        if self.run_state != "waiting" or self.current_site != "korail" or self._closed or self._macro_dialog is not None:
            return
        self._set_run_state("running")
        self.status.set("코레일 영수증 수집을 마무리하고 있습니다.")
        try:
            self.worker.finish_receipts()
        except Exception:
            self._set_run_state("waiting")
            self.status.set("수집 완료를 전달하지 못했습니다. 다시 눌러 주세요.")

    def _retry_korail(self) -> None:
        if self.run_state != "waiting" or self.current_site != "korail" or self._closed or self._macro_dialog is not None:
            return
        if self._browserless:
            self._open_korail()
            return
        self._set_run_state("running")
        self.status.set("로그인한 코레일 이용내역을 다시 조회하고 있습니다.")
        try:
            self.worker.retry_korail()
        except Exception:
            self._set_run_state("waiting")
            self.status.set("코레일 이용내역 조회를 시작하지 못했습니다. 다시 눌러 주세요.")

    def _skip(self) -> None:
        if self.run_state == "waiting" and self._macro_dialog is None:
            self._set_run_state("running")
            if self._next_site:
                next_label = SITE_LABELS.get(self._next_site, self._next_site)
                self.status.set(f"현재 항목을 건너뛰고 {next_label} 준비 중입니다.")
                self.manual_instruction.set(f"다음 항목은 {next_label}입니다. 이 창에서 진행 상태를 계속 확인하세요.")
            else:
                self.status.set("마지막 항목을 건너뛰고 보고서를 정리하고 있습니다.")
                self.manual_instruction.set("추가로 선택한 사이트가 없습니다. 저장한 자료의 보고서를 만든 후 결과를 표시합니다.")
            self.worker.skip_current()

    def _update_progress_title(self) -> None:
        short_labels = {"naver": "지도", "opinet": "오피넷", "hipass": "하이패스", "korail": "코레일"}
        mode_label = TRANSPORT_LABELS.get(self.transport_mode.get(), "")
        if self._step_index and self._step_site:
            current = short_labels.get(self._step_site, self._step_site)
            following = f"다음: {short_labels.get(self._next_site, self._next_site)}" if self._next_site else "마지막 항목"
            title = f"{mode_label} · {self._step_index}/{self._step_total} {current} · {following}"
        else:
            order = " → ".join(short_labels.get(site, site) for site in self._requested_sites)
            title = f"{mode_label} · 선택 순서: {order}"
        self.progress_frame.configure(text=title)
        last = self._step_total > 0 and self._step_index >= self._step_total
        self.skip_button.configure(text="마지막 항목 건너뛰기" if last else "이 사이트 건너뛰기")

    def _infer_step(self, site: str) -> None:
        if site in self._requested_sites and site != self._step_site:
            self._step_site = site
            self._step_index = self._requested_sites.index(site) + 1
            self._step_total = len(self._requested_sites)
            self._next_site = self._requested_sites[self._step_index] if self._step_index < self._step_total else ""
            self._update_progress_title()

    def _cancel(self) -> None:
        if self.run_state != "idle":
            self.worker.cancel()
            self.status.set("중지 요청을 전달했습니다. 진행 중인 작업을 정리합니다.")
            self.capture_button.state(["disabled"])
            self.skip_button.state(["disabled"])
            self.cancel_button.state(["disabled"])
            self.page_combo.state(["disabled"])
            self.refresh_button.state(["disabled"])
            self.external_button.state(["disabled"])
            self.pdf_button.state(["disabled"])
            self.finish_button.state(["disabled"])
            self.korail_retry_button.state(["disabled"])

    def _set_run_state(self, state: str) -> None:
        if state not in ("idle", "running", "waiting"):
            return
        self.run_state = state
        macro_open = self._macro_dialog is not None
        for widget in self.input_controls:
            if state == "idle" and not macro_open:
                widget.state(["!disabled"])
                if isinstance(widget, ttk.Combobox):
                    widget.state(["readonly"])
            else:
                widget.state(["disabled"])
        self.start_button.state(["!disabled"] if state == "idle" and not macro_open else ["disabled"])
        self.skip_button.state(["!disabled"] if state == "waiting" and not macro_open else ["disabled"])
        self.cancel_button.state(["disabled"] if state == "idle" or macro_open else ["!disabled"])
        self.macro_button.state(["disabled"] if self._closed or self._macro_busy or (state == "running" and not macro_open) else ["!disabled"])
        self.workflow_button.state(["!disabled"] if state == "idle" and not macro_open and not self._closed else ["disabled"])
        self.report_button.state(["!disabled"] if self.last_report and not macro_open and not self._closed else ["disabled"])
        self.folder_button.state(["!disabled"] if self.last_output and not macro_open and not self._closed else ["disabled"])
        self._sync_page_controls()

    def _poll_events(self) -> None:
        if self._closed:
            return
        # A bounded batch keeps the window responsive even during verbose work.
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_event(event)
            except Exception as exc:
                self._log(f"표시 처리 오류: {exc}")
        self.root.after(120, self._poll_events)

    def _handle_event(self, event: dict) -> None:
        kind = event.get("type")
        site = str(event.get("site", "") or "")
        site_label = SITE_LABELS.get(site, site)
        if site:
            self.current_site = site
        if kind == "log":
            self._log(str(event.get("message", "")))
        elif kind == "step":
            sites = event.get("sites")
            if isinstance(sites, (list, tuple)):
                self._requested_sites = tuple(value for value in sites if isinstance(value, str) and value in SITE_LABELS)
            self._step_site = site
            self._step_index = max(0, int(event.get("index", 0)))
            self._step_total = max(0, int(event.get("total", len(self._requested_sites))))
            self._next_site = str(event.get("next_site", "") or "")
            self._update_progress_title()
        elif kind == "status":
            self._infer_step(site)
            message = self._safe_message(str(event.get("message", "")))
            self.status.set(f"{site_label} · {message}" if site_label else message)
        elif kind == "manual":
            self._infer_step(site)
            if site != self._manual_site:
                self._page_ids.clear()
                self.page_selection.set("")
            self._manual_site = site
            self._browserless = bool(event.get("browserless", False))
            if self._browserless:
                self._receipt_count = max(0, int(event.get("collected_count", self._receipt_count)))
            self._set_run_state("waiting")
            self.status.set("코레일 영수증 추가 대기 · PDF 또는 출력창을 추가하세요." if self._browserless else f"{site_label or '현재 사이트'} · 화면 준비를 기다리고 있습니다.")
            self.manual_instruction.set(self._safe_message(str(event.get("message", "브라우저에서 저장할 화면을 준비한 후 ‘현재 화면 저장’을 누르세요."))))
            if self._browserless:
                self._page_ids.clear()
                self.page_selection.set("")
                self._sync_page_controls()
                if not self._korail_browser_opened:
                    self._korail_browser_opened = True
                    self._open_korail()
            else:
                self._refresh_pages()
        elif kind == "pages":
            selected_id = self._page_ids.get(self.page_selection.get())
            labels_by_id = {}
            pages = event.get("pages", [])
            self._page_ids.clear()
            for number, page in enumerate(pages, 1):
                page_id = str(page.get("id", ""))
                if not page_id:
                    continue
                title = str(page.get("title", "제목 없음")).strip() or "제목 없음"
                url = str(page.get("url", "")).split("?", 1)[0].split("#", 1)[0]
                label = f"{number}. {title[:42]} · {url[:58]}"
                self._page_ids[label] = page_id
                labels_by_id[page_id] = label
            if selected_id not in labels_by_id:
                selected_id = str(event.get("selected_id", ""))
            self.page_combo.configure(values=list(self._page_ids))
            self.page_selection.set(labels_by_id.get(selected_id, ""))
            self._sync_page_controls()
        elif kind == "captured":
            self._log(f"{site_label or '화면'} 저장 완료: {event.get('path', '')}")
        elif kind == "done":
            self._set_run_state("idle")
            report = event.get("report_path")
            output = event.get("output_dir")
            self.last_report = Path(report) if report else None
            self.last_output = Path(output) if output else None
            self.report_button.state(["!disabled"] if self.last_report and self._macro_dialog is None else ["disabled"])
            self.folder_button.state(["!disabled"] if self.last_output and self._macro_dialog is None else ["disabled"])
            count = max(0, int(event.get("count", 0)))
            site_count = max(0, int(event.get("site_count", count)))
            expected = len(self._requested_sites)
            if count == 0:
                self.status.set("저장된 화면 없음 · 확인 필요")
                self.manual_instruction.set("증빙 화면이 저장되지 않았습니다. 작업 기록과 보고서를 확인하고, 브라우저에서 조회 화면을 준비해 다시 실행하세요.")
            elif site_count < expected:
                self.status.set(f"일부 자료 저장 · 선택한 {expected}개 중 {site_count}개 사이트 · 화면·페이지 {count}개")
                self.manual_instruction.set("보고서에서 누락된 사이트를 확인하세요. 필요한 사이트를 선택해 다시 실행할 수 있습니다.")
            else:
                self.status.set(f"작업 완료 · 화면·페이지 {count}개를 저장했습니다.")
                self.manual_instruction.set("결과 폴더와 HTML 보고서에서 저장한 증빙을 확인하세요.")
            skipped = [value for value in event.get("skipped_sites", []) if isinstance(value, str) and value in SITE_LABELS]
            if skipped:
                skipped_labels = ", ".join(SITE_LABELS[value] for value in skipped)
                if count:
                    self.status.set(f"처리 완료 · 저장 {site_count}/{expected}개 사이트 · 화면·페이지 {count}개 · 건너뜀 {len(skipped)}개")
                self.manual_instruction.set(f"건너뛴 항목: {skipped_labels}. " + self.manual_instruction.get())
                self._log(f"사용자가 건너뛴 항목: {skipped_labels}")
            self.progress_frame.configure(text=f"{TRANSPORT_LABELS[self.transport_mode.get()]} · 결과 확인")
            self._log(f"보고서: {report}" if report else "작업을 마쳤습니다.")
        elif kind == "error":
            message = self._safe_message(str(event.get("message", "알 수 없는 오류가 발생했습니다.")))
            self._log(f"오류: {message}")
            self.status.set(message)
        elif kind == "state":
            self._set_run_state(str(event.get("value", "idle")))

    def _safe_message(self, message: str) -> str:
        password = self.hipass_password.get()
        return message.replace(password, "[비밀번호 숨김]") if password else message

    def _log(self, message: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", f"[{datetime.now():%H:%M:%S}] {self._safe_message(message)}\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def _open_path(self, path: Path | None) -> None:
        if path is None or self._macro_dialog is not None:
            return
        if not path.exists():
            messagebox.showerror("결과 열기", "결과 파일 또는 폴더를 찾을 수 없습니다.", parent=self.root)
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(path.resolve()))
            else:
                webbrowser.open(path.resolve().as_uri())
        except Exception as exc:
            messagebox.showerror("결과 열기", str(exc), parent=self.root)

    def _open_report(self) -> None:
        self._open_path(self.last_report)

    def _open_output(self) -> None:
        self._open_path(self.last_output)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closing_macro = self._macro_dialog
        if self._closing_macro is not None:
            self._closing_macro.close()
        self.hipass_password.set("")
        self._set_run_state("running")
        self.capture_button.state(["disabled"])
        self.skip_button.state(["disabled"])
        self.cancel_button.state(["disabled"])
        self.folder_button.state(["disabled"])
        self.report_button.state(["disabled"])
        self.status.set("브라우저를 정리하고 프로그램을 종료합니다.")
        self.manual_instruction.set("브라우저의 저장 작업과 종료 처리가 끝나면 이 창도 닫힙니다.")
        self._close_deadline = time.monotonic() + 8.0
        try:
            self.worker.shutdown()
        except Exception:
            self._log("브라우저 종료 요청을 마치지 못했습니다. 창을 종료합니다.")
        self._wait_for_worker_close()

    def _wait_for_worker_close(self) -> None:
        if self.worker.is_alive() or (self._closing_macro is not None and self._closing_macro.is_busy()):
            if time.monotonic() >= self._close_deadline:
                self.status.set("브라우저 응답을 기다리는 중입니다.")
                self.manual_instruction.set("진행 중인 조회를 마치고 브라우저를 안전하게 닫고 있습니다.")
            self.root.after(100, self._wait_for_worker_close)
            return
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    TripCaptureApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
