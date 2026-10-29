"""User-managed click macros for an already signed-in ordinary browser."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .browser_macro import BrowserMacro, ClickStep, MacroProfile, load_profile, save_profile


class BrowserMacroDialog(tk.Toplevel):
    """Keep macro jobs separate from the evidence browser and report worker."""

    def __init__(self, parent: tk.Misc, on_busy: Callable[[bool], None],
                 on_close: Callable[[], None] | None = None) -> None:
        super().__init__(parent)
        self.parent_window = parent.winfo_toplevel()
        self.on_busy = on_busy
        self.on_close = on_close
        self.events: queue.Queue = queue.Queue()
        self.core = BrowserMacro(self.events)
        self.profile: MacroProfile | None = None
        self._windows = {}
        self._busy = False
        self._notified_busy = False
        self._closed = False
        self._job_id: str | None = None
        self._terminal_handled = False
        self._poll_after_id: str | None = None
        self.selection = tk.StringVar()
        self.maximize_window = tk.BooleanVar(value=True)
        self._recording_window_mode = "maximize"
        self.step_name = tk.StringVar(value="조회 버튼")
        self.wait_seconds = tk.StringVar(value="1.0")
        self.message = tk.StringVar(value="로그인한 브라우저 창을 선택하고 버튼 위치를 한 단계씩 등록하세요.")
        self.title("브라우저 버튼 매크로")
        self.geometry("800x620")
        self.minsize(750, 590)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", self._escape)
        self._build_ui()
        self._refresh_windows()
        self._poll_after_id = self.after(80, self._poll_events)
        self._grab()

    def _build_ui(self) -> None:
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(3, weight=1)
        intro = ttk.Frame(body)
        intro.grid(row=0, column=0, sticky="ew")
        ttk.Label(intro, text="로그인한 브라우저의 조회·출력 버튼을 순서대로 클릭", font=("맑은 고딕", 13, "bold")).pack(anchor="w")
        ttk.Label(intro, text="로그인·인증은 먼저 직접 완료하세요. 페이지 확대율·스크롤·화면 배치는 직접 유지하세요.\n"
                  "클릭만 수행하며 조회 성공이나 영수증 저장 여부는 직접 확인해야 합니다.", wraplength=740).pack(anchor="w", pady=(6, 9))
        windows = ttk.Frame(body)
        windows.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        windows.columnconfigure(0, weight=1)
        ttk.Label(windows, text="이미 열려 있는 브라우저 창 · 직접 선택").grid(row=0, column=0, sticky="w")
        self.window_combo = ttk.Combobox(windows, textvariable=self.selection, state="readonly")
        self.window_combo.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(4, 0))
        self.window_combo.bind("<<ComboboxSelected>>", lambda _event: self._window_changed())
        self.refresh_button = ttk.Button(windows, text="창 새로고침", command=self._refresh_windows)
        self.refresh_button.grid(row=1, column=1, pady=(4, 0))
        self.maximize_check = ttk.Checkbutton(windows, text="기록·실행 전에 선택한 창 자동 최대화", variable=self.maximize_window,
                                             command=self._window_mode_changed)
        self.maximize_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(windows, text="해제: 기록한 창 크기로 자동 맞춤 · 위치 등록 후 설정 잠금 · 변경하려면 ‘새 매크로’", wraplength=735).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(2, 0))
        editor = ttk.Frame(body)
        editor.grid(row=2, column=0, sticky="ew", pady=(0, 9))
        editor.columnconfigure(1, weight=1)
        ttk.Label(editor, text="단계 이름").grid(row=0, column=0, padx=(0, 7))
        self.name_entry = ttk.Entry(editor, textvariable=self.step_name)
        self.name_entry.grid(row=0, column=1, sticky="ew", padx=(0, 14))
        ttk.Label(editor, text="클릭 후 대기(초)").grid(row=0, column=2, padx=(0, 7))
        self.wait_spin = ttk.Spinbox(editor, textvariable=self.wait_seconds, from_=0.2, to=30, increment=0.2, width=7)
        self.wait_spin.grid(row=0, column=3)
        buttons = ttk.Frame(editor)
        buttons.grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.record_button = ttk.Button(buttons, text="4초 후 포인터 위치 등록", command=self._record)
        self.record_button.pack(side="left", padx=(0, 7))
        self.update_button = ttk.Button(buttons, text="이름·대기 수정", command=self._update_step)
        self.update_button.pack(side="left", padx=(0, 7))
        self.remove_button = ttk.Button(buttons, text="삭제", command=self._remove_step)
        self.remove_button.pack(side="left", padx=(0, 7))
        self.up_button = ttk.Button(buttons, text="위로", command=lambda: self._move_step(-1))
        self.up_button.pack(side="left", padx=(0, 7))
        self.down_button = ttk.Button(buttons, text="아래로", command=lambda: self._move_step(1))
        self.down_button.pack(side="left")
        table = ttk.Frame(body)
        table.grid(row=3, column=0, sticky="nsew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("number", "name", "point", "wait"), show="headings", selectmode="browse", height=7)
        for key, title, width in (("number", "순서", 50), ("name", "단계 이름", 270), ("point", "창 내부 위치 x, y", 150), ("wait", "클릭 후 대기", 120)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=40, stretch=key == "name")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._step_selected())
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        status = ttk.Frame(body)
        status.grid(row=4, column=0, sticky="ew", pady=(9, 8))
        ttk.Label(status, textvariable=self.message, wraplength=735).pack(anchor="w")
        ttk.Label(status, text="창 준비 후 4초 동안 대상 버튼 위에 포인터를 두세요. 위치 등록은 클릭 녹화가 아닙니다.\n"
                  "창이 앞에 오지 않으면 4초 안에 직접 선택하세요. 실패하면 창을 클릭한 뒤 재시도하세요.\n"
                  "실행 중 Esc 또는 주 모니터 왼쪽 위로 포인터를 이동하면 중지합니다.", wraplength=735).pack(anchor="w", pady=(5, 0))
        footer = ttk.Frame(body)
        footer.grid(row=5, column=0, sticky="ew")
        self.new_button = ttk.Button(footer, text="새 매크로", command=self._new_profile)
        self.new_button.pack(side="left", padx=(0, 7))
        self.import_button = ttk.Button(footer, text="JSON 가져오기", command=self._import_profile)
        self.import_button.pack(side="left", padx=(0, 7))
        self.export_button = ttk.Button(footer, text="JSON 내보내기", command=self._export_profile)
        self.export_button.pack(side="left")
        self.close_button = ttk.Button(footer, text="닫기", command=self.close)
        self.close_button.pack(side="right")
        self.stop_button = ttk.Button(footer, text="중지", command=self._stop)
        self.stop_button.pack(side="right", padx=(0, 7))
        self.play_button = ttk.Button(footer, text="4초 후 실행", command=self._play)
        self.play_button.pack(side="right", padx=(0, 7))

    def is_busy(self) -> bool:
        # The main app may ask after this Toplevel has been destroyed, while
        # the cancelled backend thread is still releasing its final input.
        return self._busy or self.core.is_busy()

    def _grab(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _refresh_windows(self) -> None:
        if self._busy or self._closed:
            return
        selected = self._windows.get(self.selection.get())
        try:
            windows = self.core.list_windows()
        except Exception as error:
            self.message.set(str(error))
            windows = []
        self._windows = {f"{index}. [{window.browser}] {window.title[:100]}": window for index, window in enumerate(windows, 1)}
        self.window_combo.configure(values=list(self._windows))
        self.selection.set(next((label for label, window in self._windows.items() if selected and selected.hwnd == window.hwnd and selected.browser == window.browser), ""))
        self._sync_controls()

    def _window_changed(self) -> None:
        window = self._windows.get(self.selection.get())
        if self.profile and window and self.profile.browser != window.browser:
            self.message.set(f"이 매크로는 {self.profile.browser}용입니다. 같은 브라우저 창을 선택하거나 ‘새 매크로’를 누르세요.")
        self._sync_controls()

    def _window_mode_changed(self) -> None:
        if self._busy or self._closed or (self.profile is not None and self.profile.steps):
            self._sync_controls()
            return
        # There are no coordinates to preserve. A changed mode starts a clean
        # geometry instead of reusing dimensions from an empty imported file.
        self.profile = None
        mode = "자동 최대화" if self.maximize_window.get() else "현재 크기로 첫 위치 등록 · 재생 시 기록한 창 크기로 자동 맞춤"
        self.message.set(mode + "을 사용합니다. 페이지 확대율·스크롤 위치는 직접 유지하세요.")
        self._render_steps()

    def _selected_index(self) -> int | None:
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _sync_controls(self) -> None:
        if self._closed:
            return
        idle = not self._busy
        window = self._windows.get(self.selection.get())
        matching = window is not None and (self.profile is None or window.browser == self.profile.browser)
        steps = self.profile.steps if self.profile else ()
        if self.profile is not None:
            self.maximize_window.set(self.profile.window_mode == "maximize")
        index = self._selected_index()
        for widget in (self.name_entry, self.wait_spin, self.refresh_button, self.new_button, self.import_button):
            widget.state(["!disabled"] if idle else ["disabled"])
        self.window_combo.state(["!disabled", "readonly"] if idle else ["disabled"])
        self.maximize_check.state(["!disabled"] if idle and not steps else ["disabled"])
        self.tree.state(["!disabled"] if idle else ["disabled"])
        self.record_button.state(["!disabled"] if idle and matching else ["disabled"])
        self.play_button.state(["!disabled"] if idle and matching and steps else ["disabled"])
        self.export_button.state(["!disabled"] if idle and self.profile is not None else ["disabled"])
        for widget in (self.update_button, self.remove_button):
            widget.state(["!disabled"] if idle and index is not None else ["disabled"])
        self.up_button.state(["!disabled"] if idle and index is not None and index > 0 else ["disabled"])
        self.down_button.state(["!disabled"] if idle and index is not None and index < len(steps) - 1 else ["disabled"])
        self.stop_button.state(["!disabled"] if self._busy else ["disabled"])

    def _read_editor(self) -> tuple[str, int]:
        name = self.step_name.get().strip()
        if not name:
            raise ValueError("단계 이름을 입력해 주세요.")
        try:
            seconds = Decimal(self.wait_seconds.get().strip())
            if not seconds.is_finite() or not Decimal("0.2") <= seconds <= Decimal("30"):
                raise ValueError
            wait_ms = int(seconds * 1000)
        except (InvalidOperation, ValueError, OverflowError):
            raise ValueError("클릭 후 대기는 0.2~30초로 입력해 주세요.") from None
        return name, wait_ms

    def _hide_for_job(self) -> None:
        self.grab_release()
        self.withdraw()

    def _restore(self) -> None:
        if self._closed:
            return
        self.deiconify()
        self.lift()
        self._grab()

    def _begin(self, start: Callable[[], str]) -> None:
        if self._busy or self._closed:
            return
        self._busy = True
        self._terminal_handled = False
        self._sync_controls()
        try:
            self.on_busy(True)
            self._notified_busy = True
            self._hide_for_job()
            self._job_id = start()
        except Exception as error:
            self.message.set(str(error))
            self._job_id = None
            self._finish_busy()
            self._restore()

    def _record(self) -> None:
        if self._busy or self._closed:
            return
        window = self._windows.get(self.selection.get())
        if window is None:
            self.message.set("위치를 등록할 브라우저 창을 직접 선택해 주세요.")
            return
        try:
            name, wait_ms = self._read_editor()
        except ValueError as error:
            self.message.set(str(error))
            return
        self._recording_window_mode = self.profile.window_mode if self.profile is not None else ("maximize" if self.maximize_window.get() else "recorded")
        self.message.set("선택한 창의 크기와 전면 배치를 준비합니다. 준비 후 4초 동안 대상 버튼 위에 포인터를 두세요.")
        self._begin(lambda: self.core.record_point(window, name, wait_ms, profile=self.profile, window_mode=self._recording_window_mode))

    def _play(self) -> None:
        if self._busy or self._closed:
            return
        window = self._windows.get(self.selection.get())
        if window is None or self.profile is None or not self.profile.steps:
            self.message.set("브라우저 창과 등록된 단계를 확인해 주세요.")
            return
        self.message.set("선택한 창의 크기와 전면 배치를 준비한 뒤 4초 후 클릭합니다. Esc로 중지할 수 있습니다.")
        self._begin(lambda: self.core.play(window, self.profile))

    def _render_steps(self, selected: int | None = None) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, step in enumerate(self.profile.steps if self.profile else ()):
            self.tree.insert("", "end", iid=str(index), values=(index + 1, step.name, f"{step.x}, {step.y}", f"{step.wait_ms / 1000:g}초"))
        if selected is not None and self.profile and 0 <= selected < len(self.profile.steps):
            self.tree.selection_set(str(selected))
            self.tree.see(str(selected))
        self._sync_controls()

    def _step_selected(self) -> None:
        if self._busy or self._closed:
            return
        index = self._selected_index()
        if index is not None and self.profile:
            step = self.profile.steps[index]
            self.step_name.set(step.name)
            self.wait_seconds.set(f"{step.wait_ms / 1000:g}")
        self._sync_controls()

    def _replace_steps(self, steps: tuple[ClickStep, ...], selected: int | None = None) -> None:
        if self.profile is not None:
            self.profile = MacroProfile(self.profile.browser, self.profile.geometry, steps, window_mode=self.profile.window_mode)
        self._render_steps(selected)

    def _update_step(self) -> None:
        index = self._selected_index()
        if self._busy or self._closed or index is None or self.profile is None:
            return
        try:
            name, wait_ms = self._read_editor()
            steps = list(self.profile.steps)
            old = steps[index]
            steps[index] = ClickStep(name, old.x, old.y, wait_ms)
            self._replace_steps(tuple(steps), index)
        except ValueError as error:
            self.message.set(str(error))

    def _remove_step(self) -> None:
        index = self._selected_index()
        if self._busy or self._closed or index is None or self.profile is None:
            return
        steps = list(self.profile.steps)
        del steps[index]
        self._replace_steps(tuple(steps), min(index, len(steps) - 1) if steps else None)

    def _move_step(self, change: int) -> None:
        index = self._selected_index()
        if self._busy or self._closed or index is None or self.profile is None:
            return
        target = index + change
        if not 0 <= target < len(self.profile.steps):
            return
        steps = list(self.profile.steps)
        steps[index], steps[target] = steps[target], steps[index]
        self._replace_steps(tuple(steps), target)

    def _new_profile(self) -> None:
        if self._busy or self._closed:
            return
        self.profile = None
        self.maximize_window.set(True)
        self.message.set("새 매크로입니다. 브라우저 창을 선택하고 위치를 등록하세요.")
        self._render_steps()

    def _import_profile(self) -> None:
        if self._busy or self._closed:
            return
        path = filedialog.askopenfilename(parent=self, title="버튼 매크로 JSON 가져오기", filetypes=(("매크로 JSON", "*.json"),))
        if not path:
            return
        try:
            self.profile = load_profile(Path(path))
            self._render_steps()
            mode = "자동 최대화" if self.profile.window_mode == "maximize" else "기록한 창 크기로 자동 맞춤"
            self.message.set(f"{len(self.profile.steps)}개 단계 · {mode}. {self.profile.browser} 창을 선택하고 배율·스크롤 위치를 확인한 뒤 실행하세요.")
        except Exception as error:
            messagebox.showerror("매크로 가져오기", str(error), parent=self)

    def _export_profile(self) -> None:
        if self._busy or self._closed or self.profile is None:
            return
        path = filedialog.asksaveasfilename(parent=self, title="버튼 매크로 JSON 내보내기", initialfile="browser-buttons.btr-macro.json",
                                          defaultextension=".btr-macro.json", filetypes=(("매크로 JSON", "*.btr-macro.json"), ("JSON", "*.json")))
        if not path:
            return
        try:
            save_profile(Path(path), self.profile)
            self.message.set("버튼 위치·이름·대기시간을 JSON으로 저장했습니다. 로그인 정보는 저장하지 않습니다.")
        except Exception as error:
            messagebox.showerror("매크로 내보내기", str(error), parent=self)

    def _stop(self) -> None:
        if not self._busy or self._closed:
            return
        self.core.cancel()
        self.message.set("중지를 요청했습니다.")

    def _escape(self, _event=None) -> str:
        self._stop()
        return "break"

    def _finish_busy(self) -> None:
        self._busy = False
        if self._notified_busy:
            self._notified_busy = False
            try:
                self.on_busy(False)
            except Exception:
                pass
        self._sync_controls()

    def _handle_event(self, event: dict) -> None:
        if self._closed or self._job_id is None or event.get("job_id") != self._job_id:
            return
        kind = event.get("type")
        if kind == "preparing":
            self.message.set(str(event.get("message", "선택한 창의 크기와 전면 배치를 준비하고 있습니다.")))
        elif kind == "countdown":
            action = "위치 등록" if event.get("mode") == "recording" else "실행"
            hint = "대상 버튼 위에 포인터를 두세요." if event.get("mode") == "recording" else "Esc로 중지할 수 있습니다."
            self.message.set(f"창 크기 조정 후 {action}까지 {event.get('remaining', '')}초 · {hint} 창이 뒤에 있으면 직접 선택하세요.")
        elif kind == "progress":
            self.message.set(f"{event.get('index', '')}/{event.get('total', '')} · {event.get('name', '')} 클릭 중")
        elif kind in ("recorded", "done", "stopped", "error") and not self._terminal_handled:
            self._terminal_handled = True
            if kind == "recorded":
                try:
                    steps = self.profile.steps if self.profile else ()
                    mode = event.get("window_mode", self._recording_window_mode)
                    if self.profile is not None and steps and mode != self.profile.window_mode:
                        raise ValueError("기존 위치의 창 설정이 바뀌었습니다.")
                    profile = MacroProfile(event["browser"], event["geometry"], steps + (event["step"],), window_mode=mode)
                except (KeyError, TypeError, ValueError, AttributeError) as error:
                    self.message.set(f"포인터 위치를 등록하지 못했습니다({type(error).__name__}). 기존 단계는 유지했습니다. 작업 종료 후 다시 등록해 주세요.")
                else:
                    self.profile = profile
                    self._render_steps(len(self.profile.steps) - 1)
                    self.message.set("포인터 위치를 등록했습니다. 실제 클릭을 기록한 것은 아닙니다.")
            elif kind == "done":
                self.message.set(f"{event.get('count', 0)}개 버튼 클릭을 마쳤습니다. 조회 결과와 영수증은 직접 확인하세요.")
            else:
                self.message.set(str(event.get("message", "작업을 중지했습니다.")))
        elif kind == "idle":
            self._job_id = None
            self._finish_busy()
            self._restore()

    def _poll_events(self) -> None:
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
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                # Invalid job payloads must not prevent the following idle
                # event from restoring the window. Tk/state failures remain
                # visible rather than being silently discarded.
                self.message.set(f"매크로 상태를 표시하지 못했습니다({type(error).__name__}). 작업이 끝난 뒤 다시 시도해 주세요.")
        if not self._closed:
            self._poll_after_id = self.after(80, self._poll_events)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._job_id = None
        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
            self._poll_after_id = None
        try:
            self.core.close()
        finally:
            self._finish_busy()
            try:
                self.grab_release()
                self.destroy()
            except tk.TclError:
                pass
            if self.on_close is not None:
                self.on_close()
