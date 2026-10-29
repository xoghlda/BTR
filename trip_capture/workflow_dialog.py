"""Editable, explicitly registered login-to-evidence workflows for a browser."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .browser_macro import MacroProfile
from .credentials import WindowsCredentialStore
from .models import RunRequest, SITE_LABELS
from .workflow import WorkflowEngine, WorkflowProfile, WorkflowStep, load_workflow, save_workflow
from .workflow_evidence import WorkflowEvidence


KINDS = {
    "open_url": "주소 열기", "click": "버튼 클릭", "text": "일반·변수 입력", "username": "아이디 입력",
    "password": "비밀번호 입력", "key": "키 입력", "pause": "인증 대기", "switch_window": "새 창 전환",
    "capture": "화면 증빙 저장", "pdf": "PDF 증빙 저장",
}
POINT_KINDS = {"click", "text", "username", "password"}
URLS = {
    "naver": "https://map.naver.com/p/directions/-/-/-/car",
    "opinet": "https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do",
    "hipass": "https://www.hipass.co.kr/usepculr/InitUsePculrTabSearch.do",
    "korail": "https://www.korail.com/ticket/mypage/ticketInfo/receipt",
}
LOGIN_URLS = {"hipass": "https://www.hipass.co.kr/comm/lginpg.do?any_tmptab=01", "korail": "https://www.korail.com/ticket/login"}


class WorkflowDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, on_busy: Callable[[bool], None], on_close: Callable[[], None] | None = None,
                 *, request: RunRequest | None = None, on_report: Callable[[Path], None] | None = None):
        super().__init__(parent)
        self.request, self.on_busy, self.on_close, self.on_report = request, on_busy, on_close, on_report
        self.events: queue.Queue = queue.Queue()
        self.core = WorkflowEngine(self.events, collector=None)
        self.profile: WorkflowProfile | None = None
        self._windows = {}
        self._busy = self._paused = self._closed = self._closing = self._notified_busy = False
        self._job_id = self._poll_after_id = None
        self._record_target = None
        self._reported: set[str] = set()
        self.selection = tk.StringVar()
        self.maximize_window = tk.BooleanVar(value=True)
        self.kind = tk.StringVar(value=KINDS["click"])
        self.name = tk.StringVar(value="조회 버튼")
        first_site = request.sites[0] if request and request.sites else "korail"
        self.site = tk.StringVar(value=SITE_LABELS[first_site])
        self.value = tk.StringVar()
        self.wait_seconds = tk.StringVar(value="1.0")
        self.pdf_path = tk.StringVar()
        self.message = tk.StringVar(value="전체흐름예시는 미검증 편집 틀입니다. 모든 입력·클릭 위치를 직접 등록해야 실행됩니다.")
        self.accounts = {site: {"username": tk.StringVar(), "password": tk.StringVar()} for site in SITE_LABELS}
        self.account_status = {site: tk.StringVar(value="이 창에서만 사용합니다. 저장·불러오기는 직접 선택하세요.") for site in SITE_LABELS}
        self.title("로그인부터 전체 흐름")
        self.geometry("800x650")
        self.minsize(760, 620)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _event: self._stop())
        self._build_ui()
        self._refresh_windows()
        self._poll_after_id = self.after(80, self._poll_events)
        self._grab()

    def _build_ui(self):
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        ttk.Label(body, text="로그인 → 조회 → 원본 증빙 → 보고서", font=("맑은 고딕", 13, "bold")).grid(row=0, column=0, sticky="w")
        window = ttk.Frame(body)
        window.grid(row=1, column=0, sticky="ew", pady=(6, 7))
        window.columnconfigure(0, weight=1)
        self.window_combo = ttk.Combobox(window, textvariable=self.selection, state="readonly")
        self.window_combo.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        self.window_combo.bind("<<ComboboxSelected>>", lambda _event: self._sync())
        self.refresh_button = ttk.Button(window, text="창 새로고침", command=self._refresh_windows)
        self.refresh_button.grid(row=0, column=1)
        self.maximize_check = ttk.Checkbutton(window, text="창 자동 최대화 · 해제하면 기록 크기로 맞춤", variable=self.maximize_window,
                                             command=self._mode_changed)
        self.maximize_check.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.notebook = ttk.Notebook(body)
        self.notebook.grid(row=2, column=0, sticky="nsew")
        steps = ttk.Frame(self.notebook, padding=7)
        accounts = ttk.Frame(self.notebook, padding=9)
        files = ttk.Frame(self.notebook, padding=9)
        self.notebook.add(steps, text="전체 흐름 편집")
        self.notebook.add(accounts, text="사이트별 계정")
        self.notebook.add(files, text="PDF·출장 변수")
        self._edit_controls = []
        self._build_steps(steps)
        self._build_accounts(accounts)
        self._build_files(files)
        ttk.Label(body, textvariable=self.message, wraplength=750).grid(row=3, column=0, sticky="w", pady=(7, 5))
        ttk.Label(body, text="인증·보안 입력은 자동 우회하지 않습니다. 새 창은 전환 단계를 추가하세요. 배율·스크롤·배치는 직접 유지하세요.", wraplength=750).grid(row=4, column=0, sticky="w")
        footer = ttk.Frame(body)
        footer.grid(row=5, column=0, sticky="ew", pady=(7, 0))
        self.new_button = ttk.Button(footer, text="새 흐름", command=self._new)
        self.new_button.pack(side="left", padx=(0, 5))
        self.import_button = ttk.Button(footer, text="JSON 가져오기", command=self._import)
        self.import_button.pack(side="left", padx=(0, 5))
        self.export_button = ttk.Button(footer, text="JSON 내보내기", command=self._export)
        self.export_button.pack(side="left")
        self.close_button = ttk.Button(footer, text="닫기", command=self.close)
        self.close_button.pack(side="right")
        self.stop_button = ttk.Button(footer, text="중지", command=self._stop)
        self.stop_button.pack(side="right", padx=(0, 5))
        self.resume_button = ttk.Button(footer, text="인증 완료·계속", command=self._resume)
        self.resume_button.pack(side="right", padx=(0, 5))
        self.play_button = ttk.Button(footer, text="전체 실행", command=self._play)
        self.play_button.pack(side="right", padx=(0, 5))

    def _build_steps(self, frame):
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        top = ttk.Frame(frame)
        top.grid(row=0, column=0, sticky="ew")
        self.template_button = ttk.Button(top, text="전체흐름예시 · 미검증 틀", command=self._template)
        self.template_button.pack(side="left", padx=(0, 6))
        self.basis_button = ttk.Button(top, text="4초 창 기준 등록", command=lambda: self._record(basis=True))
        self.basis_button.pack(side="left")
        ttk.Label(frame, text="예시의 ‘미등록’ 좌표를 모두 등록하세요. 아이디·비밀번호는 값 칸 대신 계정 탭을 사용합니다.", wraplength=730).grid(row=1, column=0, sticky="w", pady=(5, 4))
        editor = ttk.Frame(frame)
        editor.grid(row=2, column=0, sticky="ew", pady=(0, 5))
        editor.columnconfigure(2, weight=1)
        self.kind_combo = ttk.Combobox(editor, textvariable=self.kind, values=list(KINDS.values()), state="readonly", width=14)
        self.kind_combo.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.kind_combo.bind("<<ComboboxSelected>>", lambda _event: self._kind_changed())
        self.site_combo = ttk.Combobox(editor, textvariable=self.site, values=list(SITE_LABELS.values()), state="readonly", width=16)
        self.site_combo.grid(row=0, column=1, padx=(0, 5))
        self.name_entry = ttk.Entry(editor, textvariable=self.name)
        self.name_entry.grid(row=0, column=2, sticky="ew")
        ttk.Label(editor, text="값·변수·창 제목 필터").grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.value_entry = ttk.Entry(editor, textvariable=self.value)
        self.value_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(5, 0))
        ttk.Label(editor, text="클릭/단계 후 대기(초)").grid(row=2, column=0, sticky="w", pady=(5, 0))
        self.wait_spin = ttk.Spinbox(editor, textvariable=self.wait_seconds, from_=0.2, to=30, increment=0.2, width=8)
        self.wait_spin.grid(row=2, column=1, sticky="w", pady=(5, 0))
        action = ttk.Frame(editor)
        action.grid(row=2, column=2, sticky="e", pady=(5, 0))
        self.add_button = ttk.Button(action, text="단계 추가", command=self._add)
        self.add_button.pack(side="left", padx=(0, 5))
        self.record_button = ttk.Button(action, text="4초 좌표 등록", command=self._record)
        self.record_button.pack(side="left")
        self.tree = ttk.Treeview(frame, columns=("kind", "name", "site", "point", "wait"), show="headings", selectmode="browse", height=6)
        for key, label, width in (("kind", "종류", 105), ("name", "단계 이름", 260), ("site", "사이트", 120), ("point", "좌표", 105), ("wait", "대기", 65)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=40, stretch=key == "name")
        self.tree.grid(row=3, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        scroll.grid(row=3, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._selected())
        bottom = ttk.Frame(frame)
        bottom.grid(row=4, column=0, sticky="w", pady=(5, 0))
        self.update_button = ttk.Button(bottom, text="선택 단계 수정", command=self._update)
        self.update_button.pack(side="left", padx=(0, 5))
        self.remove_button = ttk.Button(bottom, text="삭제", command=self._remove)
        self.remove_button.pack(side="left", padx=(0, 5))
        self.up_button = ttk.Button(bottom, text="위로", command=lambda: self._move(-1))
        self.up_button.pack(side="left", padx=(0, 5))
        self.down_button = ttk.Button(bottom, text="아래로", command=lambda: self._move(1))
        self.down_button.pack(side="left")
        self._edit_controls.extend((self.kind_combo, self.site_combo, self.name_entry, self.value_entry, self.wait_spin, self.template_button,
                                    self.add_button, self.update_button, self.remove_button, self.up_button, self.down_button))

    def _build_accounts(self, frame):
        ttk.Label(frame, text="사용할 사이트의 계정만 입력하세요. 비밀번호는 흐름 JSON·설정·작업 기록에 넣지 않습니다.\n저장하지 않으면 닫을 때 입력값을 비웁니다. 보안 키보드·추가 인증은 인증 대기에서 직접 진행하세요.", wraplength=720).pack(anchor="w", pady=(0, 9))
        tabs = ttk.Notebook(frame)
        tabs.pack(fill="both", expand=True)
        for site, label in SITE_LABELS.items():
            tab = ttk.Frame(tabs, padding=12)
            tabs.add(tab, text=label)
            tab.columnconfigure(1, weight=1)
            for row, key, caption in ((0, "username", "아이디"), (1, "password", "비밀번호")):
                ttk.Label(tab, text=caption).grid(row=row, column=0, sticky="w", padx=(0, 9), pady=5)
                field = ttk.Entry(tab, textvariable=self.accounts[site][key], show="●" if key == "password" else "")
                field.grid(row=row, column=1, sticky="ew", pady=5)
                self._edit_controls.append(field)
            actions = ttk.Frame(tab)
            actions.grid(row=2, column=0, columnspan=2, sticky="w", pady=10)
            for caption, method in (("이 PC에서 불러오기", self._load_account), ("이 PC에 저장", self._save_account), ("저장 정보 삭제", self._delete_account)):
                button = ttk.Button(actions, text=caption, command=lambda s=site, m=method: m(s))
                button.pack(side="left", padx=(0, 7))
                self._edit_controls.append(button)
            ttk.Label(tab, textvariable=self.account_status[site], wraplength=680).grid(row=3, column=0, columnspan=2, sticky="w")

    def _build_files(self, frame):
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="receipt_pdf · 이번 실행에서 출력할 PDF의 정확한 파일 경로", wraplength=720).grid(row=0, column=0, sticky="w")
        path = ttk.Entry(frame, textvariable=self.pdf_path)
        path.grid(row=1, column=0, sticky="ew", pady=5)
        choose = ttk.Button(frame, text="출력 PDF 파일 경로 지정", command=self._choose_pdf)
        choose.grid(row=1, column=1, padx=(7, 0))
        self._edit_controls.extend((path, choose))
        ttk.Label(frame, text="pdf 단계 값에는 receipt_pdf를 쓰세요. 폴더를 훑지 않고 지정한 파일만 최대 60초 확인합니다.\n이번 실행 중 새로 생성되거나 변경된 PDF만 대상입니다. 출력 경로가 다르면 직접 수정하세요.\n새 창은 switch_window 단계에 창 제목 일부를 넣고, 인증은 pause 단계에서 직접 완료합니다.", wraplength=720).grid(row=2, column=0, columnspan=2, sticky="w", pady=(5, 12))
        values = self._variables()
        text = "출장 변수: " + ", ".join("{" + key + "}" for key in values)
        ttk.Label(frame, text=text, wraplength=720).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="값 예: {trip_date} · {trip_date_compact}. 사이트가 요구하는 날짜 형식과 입력 항목을 확인하세요.\nkey 단계: ENTER, TAB, SHIFT+TAB, CTRL+A, CTRL+P, ESC, SPACE, DOWN, UP, HOME, END", wraplength=720).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _variables(self):
        request = self.request
        end_date = (request.trip_end_date or request.trip_date) if request else ""
        return {"trip_date": request.trip_date if request else "", "trip_end_date": end_date,
                "trip_end_date_compact": end_date.replace("-", ""),
                "trip_date_compact": request.trip_date.replace("-", "") if request else "", "origin": request.origin if request else "",
                "destination": request.destination if request else "", "rail_origin": request.rail_origin if request else "",
                "rail_destination": request.rail_destination if request else ""}

    def is_busy(self):
        return self._busy or self.core.is_busy()

    def _grab(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _safe(self, message):
        text = str(message)
        secrets = [variable.get() for account in self.accounts.values() for variable in account.values() if variable.get()]
        for secret in sorted(set(secrets), key=len, reverse=True):
            text = text.replace(secret, "[계정 정보 숨김]")
        return text

    def _index(self):
        selected = self.tree.selection()
        return int(selected[0]) if selected else None

    def _code(self):
        return next(key for key, label in KINDS.items() if label == self.kind.get())

    def _site(self):
        return next(key for key, label in SITE_LABELS.items() if label == self.site.get())

    def _refresh_windows(self):
        if self._busy or self._closed:
            return
        old = self._windows.get(self.selection.get())
        try:
            windows = self.core.list_windows()
        except Exception:
            windows = []
            self.message.set("브라우저 창 목록을 확인하지 못했습니다. 창을 연 뒤 다시 새로고침하세요.")
        self._windows = {f"{i}. [{w.browser}] {w.title[:85]}": w for i, w in enumerate(windows, 1)}
        self.window_combo.configure(values=list(self._windows))
        self.selection.set(next((label for label, w in self._windows.items() if old and old.hwnd == w.hwnd and old.browser == w.browser), ""))
        self._sync()

    def _sync(self):
        if self._closed:
            return
        idle = not self._busy
        for widget in self._edit_controls:
            widget.state(["!disabled"] if idle else ["disabled"])
            if idle and isinstance(widget, ttk.Combobox):
                widget.state(["readonly"])
        for widget in (self.new_button, self.import_button, self.export_button, self.refresh_button):
            widget.state(["!disabled"] if idle else ["disabled"])
        self.window_combo.state(["!disabled", "readonly"] if idle else ["disabled"])
        if self.profile:
            self.maximize_window.set(self.profile.window_mode == "maximize")
        self.maximize_check.state(["!disabled"] if idle and (not self.profile or self.profile.geometry is None) else ["disabled"])
        window = self._windows.get(self.selection.get())
        compatible = window and (not self.profile or self.profile.geometry is None or window.browser == self.profile.browser)
        self.record_button.state(["!disabled"] if idle and compatible and self._code() in POINT_KINDS else ["disabled"])
        self.basis_button.state(["!disabled"] if idle and compatible else ["disabled"])
        self.play_button.state(["!disabled"] if idle and compatible and self.profile and self.profile.steps and self.request else ["disabled"])
        self.resume_button.state(["!disabled"] if self._paused and not self._closing else ["disabled"])
        self.stop_button.state(["!disabled"] if self._busy else ["disabled"])
        self.tree.state(["!disabled"] if idle else ["disabled"])
        if idle:
            self._kind_changed()

    def _kind_changed(self):
        if self._busy or self._closed:
            return
        kind = self._code()
        secret = kind in ("username", "password")
        if secret:
            self.value.set("")
        self.value_entry.state(["disabled"] if secret else ["!disabled"])
        window = self._windows.get(self.selection.get())
        compatible = window and (not self.profile or self.profile.geometry is None or window.browser == self.profile.browser)
        self.record_button.state(["!disabled"] if kind in POINT_KINDS and compatible else ["disabled"])

    def _mode_changed(self):
        if self._busy or (self.profile and self.profile.geometry is not None):
            self._sync()
            return
        if self.profile:
            self.profile = WorkflowProfile(self.profile.browser, None, self.profile.steps, "maximize" if self.maximize_window.get() else "recorded")
        self._sync()

    def _make_step(self, *, x=None, y=None):
        kind, site = self._code(), self._site()
        seconds = Decimal(self.wait_seconds.get())
        if not seconds.is_finite() or not Decimal("0.2") <= seconds <= 30:
            raise ValueError("단계 대기는 0.2~30초로 입력하세요.")
        value = "" if kind in ("username", "password", "click", "capture") else self.value.get()
        if kind == "pdf":
            value = value.strip() or "receipt_pdf"
        return WorkflowStep(kind, self.name.get().strip(), site,
                            (-1 if kind in POINT_KINDS else 0) if x is None else x,
                            (-1 if kind in POINT_KINDS else 0) if y is None else y, value, int(seconds * 1000))

    def _set_steps(self, steps, selected=None):
        window = self._windows.get(self.selection.get())
        browser = self.profile.browser if self.profile else (window.browser if window else ("Chrome" if self.request and self.request.browser == "chrome" else "Edge"))
        geometry = self.profile.geometry if self.profile else None
        mode = self.profile.window_mode if self.profile else ("maximize" if self.maximize_window.get() else "recorded")
        self.profile = WorkflowProfile(browser, geometry, tuple(steps), mode)
        self._render(selected)

    def _render(self, selected=None):
        self.tree.delete(*self.tree.get_children())
        for index, step in enumerate(self.profile.steps if self.profile else ()):
            point = "미등록" if step.kind in POINT_KINDS and (step.x < 0 or step.y < 0) else (f"{step.x}, {step.y}" if step.kind in POINT_KINDS else "—")
            self.tree.insert("", "end", iid=str(index), values=(KINDS[step.kind], self._safe(step.name), SITE_LABELS[step.site], point, f"{step.wait_ms / 1000:g}초"))
        if selected is not None and self.profile and 0 <= selected < len(self.profile.steps):
            self.tree.selection_set(str(selected))
            self.tree.see(str(selected))
        self._sync()

    def _selected(self):
        if self._busy or self._closed:
            return
        index = self._index()
        if index is not None and self.profile:
            step = self.profile.steps[index]
            self.kind.set(KINDS[step.kind]); self.site.set(SITE_LABELS[step.site]); self.name.set(step.name)
            self.value.set(step.value); self.wait_seconds.set(f"{step.wait_ms / 1000:g}")
        self._sync()

    def _add(self):
        if self._busy or self._closed:
            return
        try:
            step = self._make_step()
            self._set_steps((self.profile.steps if self.profile else ()) + (step,), len(self.profile.steps) if self.profile else 0)
        except (ValueError, InvalidOperation, StopIteration) as error:
            self.message.set(self._safe(error))

    def _update(self):
        index = self._index()
        if self._busy or self._closed or index is None or not self.profile:
            return
        try:
            old = self.profile.steps[index]
            preserve = old.kind == self._code() and old.site == self._site()
            step = self._make_step(x=old.x if preserve else None, y=old.y if preserve else None)
            steps = list(self.profile.steps); steps[index] = step
            self._set_steps(steps, index)
        except (ValueError, InvalidOperation) as error:
            self.message.set(self._safe(error))

    def _remove(self):
        index = self._index()
        if self._busy or self._closed or index is None or not self.profile:
            return
        steps = list(self.profile.steps); del steps[index]
        self._set_steps(steps, min(index, len(steps) - 1) if steps else None)

    def _move(self, delta):
        index = self._index()
        if self._busy or self._closed or index is None or not self.profile or not 0 <= index + delta < len(self.profile.steps):
            return
        steps = list(self.profile.steps); target = index + delta
        steps[index], steps[target] = steps[target], steps[index]
        self._set_steps(steps, target)

    def _template(self):
        if self._busy or self._closed:
            return
        sites = self.request.sites if self.request else (self._site(),)
        steps = []
        def add(kind, name, site, value=""):
            point = -1 if kind in POINT_KINDS else 0
            steps.append(WorkflowStep(kind, name, site, point, point, value, 1000))
        for site in sites:
            if site in LOGIN_URLS:
                add("open_url", "공식 로그인 진입", site, LOGIN_URLS[site])
                add("username", "아이디 입력 · 위치 등록 필요", site)
                add("password", "비밀번호 입력 · 위치 등록 필요", site)
                add("click", "로그인 버튼 · 위치 등록 필요", site)
                add("pause", "로그인·추가 인증 직접 확인", site, "인증을 직접 완료한 뒤 계속을 누르세요.")
            add("open_url", "공식 조회 화면 열기", site, URLS[site])
            if site == "naver":
                add("text", "출발지 입력", site, "{origin}")
                add("click", "출발지 후보 선택", site)
                add("text", "도착지 입력", site, "{destination}")
                add("click", "도착지 후보 선택", site)
            else:
                add("text", "시작일 조건 · 형식 확인", site, "{trip_date}")
                add("text", "종료일 조건 · 형식 확인", site, "{trip_end_date}")
            add("click", "조회 버튼 · 위치 등록 필요", site)
            if site in LOGIN_URLS:
                add("click", "개별 영수증 선택·열기", site)
            add("capture", "조회 결과·영수증 원본 저장", site)
        self.profile = None
        self.maximize_window.set(True)
        self._set_steps(steps)
        self.message.set("미검증 전체 예시입니다. 사이트의 실제 로그인·조회 화면에서 모든 미등록 위치와 날짜 형식을 수정하세요. 새 창/PDF는 전환/pdf 단계를 직접 추가하세요.")

    def _begin(self, start):
        if self._busy or self._closed:
            return
        self._busy = True; self._paused = False
        self._sync()
        try:
            self._notified_busy = True
            self.on_busy(True)
            self.grab_release(); self.withdraw()
            self._job_id = start()
        except Exception as error:
            self.message.set(self._safe(error))
            self._finish_busy(); self._restore()

    def _record(self, basis=False):
        if self._busy or self._closed:
            return
        window = self._windows.get(self.selection.get())
        if not window:
            self.message.set("기록할 브라우저 창을 선택하세요.")
            return
        try:
            step = None if basis else self._make_step()
            if step and step.kind not in POINT_KINDS:
                raise ValueError("좌표 등록은 클릭·일반 입력·아이디·비밀번호 단계에 사용하세요.")
            self._record_target = (None if basis else self._index(), step)
            guard = MacroProfile(self.profile.browser, self.profile.geometry, (), self.profile.window_mode) if self.profile and self.profile.geometry else None
            mode = self.profile.window_mode if self.profile else ("maximize" if self.maximize_window.get() else "recorded")
        except (ValueError, InvalidOperation) as error:
            self.message.set(self._safe(error))
            return
        self._begin(lambda: self.core.record_point(window, step.name if step else "창 기준 등록", step.wait_ms if step else 1000,
                                                  profile=guard, window_mode=mode))

    def _play(self):
        if self._busy or self._closed:
            return
        try:
            if self.request is None:
                raise ValueError("메인 창의 출장 정보를 먼저 입력하세요. 현재는 편집만 가능합니다.")
            self.request.validate()
            window = self._windows.get(self.selection.get())
            if not window or not self.profile or not self.profile.steps:
                raise ValueError("대상 창과 전체 흐름 단계를 준비하세요.")
            if self.profile.geometry is None:
                raise ValueError("좌표 또는 창 기준을 먼저 등록하세요.")
            accounts = {site: (fields["username"].get(), fields["password"].get()) for site, fields in self.accounts.items()}
            for step in self.profile.steps:
                if step.site not in self.request.sites:
                    raise ValueError("메인 창에서 선택한 사이트 밖의 단계는 실행할 수 없습니다.")
                if step.kind in POINT_KINDS and (step.x < 0 or step.y < 0):
                    raise ValueError("미등록 입력·클릭 위치가 있습니다. 각 위치를 직접 등록하세요.")
                if step.kind in ("username", "password") and not all(accounts[step.site]):
                    raise ValueError("아이디·비밀번호 단계의 사이트별 계정을 먼저 입력하세요.")
            self._check_no_secrets()
            paths = {"receipt_pdf": Path(self.pdf_path.get()).expanduser()} if self.pdf_path.get().strip() else {}
            for step in self.profile.steps:
                if step.kind == "pdf":
                    path = paths.get(step.value or "receipt_pdf")
                    if path is None or path.suffix.lower() != ".pdf" or path.is_dir():
                        raise ValueError("PDF·출장 변수 탭에서 이번 실행의 정확한 출력 PDF 파일 경로를 지정하세요.")
            self._reported.clear()
        except (ValueError, OSError) as error:
            self.message.set(self._safe(error))
            return
        def start():
            self.core.close()
            self.core = WorkflowEngine(self.events, collector=WorkflowEvidence(self.request))
            return self.core.play(window, self.profile, accounts=accounts, variables=self._variables(), pdf_paths=paths)
        self._begin(start)

    def _check_no_secrets(self):
        if not self.profile:
            return
        for fields in self.accounts.values():
            for variable in fields.values():
                secret = variable.get()
                if secret and any(secret in step.name or secret in step.value for step in self.profile.steps):
                    raise ValueError("계정 정보가 단계 이름·값에 들어 있습니다. 해당 값은 지우고 사이트별 계정 탭만 사용하세요.")

    def _resume(self):
        if not self._paused or self._closed or self._closing:
            return
        self._paused = False
        self.grab_release(); self.withdraw()
        self._sync()
        try:
            if self.core.resume(self._job_id) is False:
                raise ValueError("인증 대기 작업이 바뀌었습니다.")
        except Exception:
            self._paused = True; self._restore(); self._sync()
            self.message.set("계속 실행을 전달하지 못했습니다. 인증 상태를 확인하고 다시 누르세요.")

    def _stop(self):
        if self._busy and not self._closed:
            self.core.cancel()

    def _restore(self):
        if not self._closed and not self._closing:
            self.deiconify(); self.lift(); self._grab()

    def _finish_busy(self):
        self._busy = self._paused = False
        self._job_id = None
        if self._notified_busy:
            self._notified_busy = False
            self.on_busy(False)
        self._sync()

    def _notify_report(self, event):
        report = event.get("report")
        if isinstance(report, (str, Path)) and str(report) and str(report) not in self._reported:
            self._reported.add(str(report))
            if self.on_report:
                self.on_report(Path(report))

    def _handle_event(self, event):
        if self._closed or self._job_id is None or event.get("job_id") != self._job_id:
            return
        kind = event.get("type")
        if kind == "recorded" and not self._closing:
            index, draft = self._record_target
            mode = event["window_mode"]
            geometry = event["geometry"]
            if self.profile and self.profile.geometry and (geometry != self.profile.geometry or mode != self.profile.window_mode):
                self.message.set("기록한 창 크기·모드와 달라 위치를 추가하지 않았습니다. 새 흐름에서 다시 등록하세요.")
                return
            steps = list(self.profile.steps if self.profile else ())
            if draft:
                point = event["step"]
                step = WorkflowStep(draft.kind, draft.name, draft.site, point.x, point.y, draft.value, draft.wait_ms)
                if index is None:
                    steps.append(step); index = len(steps) - 1
                else:
                    steps[index] = step
            self.profile = WorkflowProfile(event["browser"], geometry, tuple(steps), mode)
            self._render(index)
            self.message.set("창 기준·좌표를 등록했습니다. 실제 입력이나 클릭은 아직 실행하지 않았습니다.")
        elif kind == "paused":
            self._paused = True
            self.message.set(self._safe(event.get("message", "인증을 직접 완료한 뒤 계속을 누르세요.")))
            self._restore(); self._sync()
        elif kind in ("done", "error", "stopped"):
            self._notify_report(event)
            self.message.set(self._safe(event.get("message", "흐름 실행을 마쳤습니다. 사이트 결과와 보고서의 원본을 확인하세요.")))
        elif kind == "idle":
            self._finish_busy()
            if self._closing:
                self._final_close()
            else:
                self._restore()
        elif kind in ("countdown", "preparing", "progress"):
            self.message.set(self._safe(event.get("message", f"흐름 준비·실행 중 {event.get('remaining', '')}")))

    def _poll_events(self):
        self._poll_after_id = None
        if self._closed:
            return
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_event(event)
            except (KeyError, ValueError, TypeError, AttributeError):
                self.message.set("작업 상태를 표시하지 못했습니다. 중지 후 등록 정보를 다시 확인하세요.")
        if not self._closed:
            self._poll_after_id = self.after(80, self._poll_events)

    def _credential_key(self, site, field):
        return f"workflow:{site}:{field}"

    def _load_account(self, site):
        if self._busy or self._closed:
            return
        try:
            store = WindowsCredentialStore()
            values = {key: store.get(self._credential_key(site, key)) for key in ("username", "password")}
            for key, value in values.items():
                self.accounts[site][key].set(value or "")
            self.account_status[site].set("이 PC의 저장 정보를 불러왔습니다." if all(values.values()) else "일부 저장 정보가 없습니다. 계정을 직접 입력하세요.")
        except Exception:
            self.account_status[site].set("저장 정보를 불러오지 못했습니다. 계정을 직접 입력하세요.")

    def _save_account(self, site):
        if self._busy or self._closed:
            return
        values = {key: variable.get() for key, variable in self.accounts[site].items()}
        if not all(values.values()):
            self.account_status[site].set("아이디와 비밀번호를 모두 입력하세요.")
            return
        store = None
        previous = None
        try:
            store = WindowsCredentialStore()
            previous = {key: store.get(self._credential_key(site, key)) for key in values}
            for key, value in values.items():
                store.set(self._credential_key(site, key), value)
            self.account_status[site].set("이 PC의 Windows 자격 증명에 저장했습니다.")
        except Exception:
            restored = True
            if store is not None and previous is not None:
                for key, old_value in previous.items():
                    try:
                        if old_value is None:
                            store.delete(self._credential_key(site, key))
                        else:
                            store.set(self._credential_key(site, key), old_value)
                    except Exception:
                        restored = False
            self.account_status[site].set("저장하지 못해 이전 저장 상태를 유지했습니다. 입력값은 이 창에만 유지합니다." if restored
                                          else "저장·복구를 마치지 못했습니다. Windows 자격 증명에서 해당 저장 정보를 확인하세요.")

    def _delete_account(self, site):
        if self._busy or self._closed:
            return
        try:
            store = WindowsCredentialStore()
            for key in ("username", "password"):
                store.delete(self._credential_key(site, key))
            self.account_status[site].set("이 PC의 해당 저장 정보를 삭제했습니다.")
        except Exception:
            self.account_status[site].set("저장 정보를 삭제하지 못했습니다. Windows 자격 증명을 확인하세요.")

    def _choose_pdf(self):
        if self._busy or self._closed:
            return
        path = filedialog.asksaveasfilename(parent=self, title="이번 실행에서 출력할 PDF 파일 경로", defaultextension=".pdf", confirmoverwrite=False,
                                          filetypes=(("PDF", "*.pdf"),))
        if path:
            self.pdf_path.set(path)

    def _new(self):
        if self._busy or self._closed:
            return
        self.profile = None; self.maximize_window.set(True); self._render()

    def _import(self):
        if self._busy or self._closed:
            return
        path = filedialog.askopenfilename(parent=self, filetypes=(("흐름 JSON", "*.json"),))
        if path:
            try:
                self.profile = load_workflow(Path(path)); self._render()
                self.message.set("편집 흐름을 가져왔습니다. 미등록 위치·계정·PDF 경로는 실행 전에 준비하세요.")
            except Exception:
                self.message.set("흐름 JSON을 읽지 못했습니다. 지원하는 편집 파일인지 확인하세요.")

    def _export(self):
        if self._busy or self._closed or not self.profile:
            return
        try:
            self._check_no_secrets()
        except ValueError as error:
            self.message.set(str(error)); return
        path = filedialog.asksaveasfilename(parent=self, initialfile="travel.btr-workflow.json", defaultextension=".btr-workflow.json",
                                          filetypes=(("흐름 JSON", "*.btr-workflow.json"),))
        if path:
            try:
                save_workflow(Path(path), self.profile)
                self.message.set("계정·PDF 파일 경로를 제외한 흐름을 저장했습니다. 미등록 위치가 남아 있으면 실행할 수 없습니다.")
            except Exception:
                self.message.set("흐름을 저장하지 못했습니다. 파일 권한과 편집 정보를 확인하세요.")

    def close(self):
        if self._closed:
            return
        if self._busy:
            self._closing = True
            self.core.cancel()
            self._sync()
            return
        self._final_close()

    def _final_close(self):
        if self._closed:
            return
        self._closed = True
        if self._poll_after_id:
            self.after_cancel(self._poll_after_id)
            self._poll_after_id = None
        self.core.close()
        for fields in self.accounts.values():
            for variable in fields.values():
                variable.set("")
        try:
            self.grab_release(); self.destroy()
        finally:
            if self.on_close:
                self.on_close()
