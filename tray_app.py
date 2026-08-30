from __future__ import annotations

import argparse
import logging
import threading
import os
from queue import Empty, Queue
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw
import pystray

from main import run_evaluation
from smu_auto_evaluation.scheduler import ScheduleState, ScheduledRun, schedule_wait_seconds
from smu_auto_evaluation.settings import APP_NAME, Settings, app_data_dir
from smu_auto_evaluation.startup import set_startup

_instance_mutex = None


def acquire_single_instance() -> bool:
    """Prevent a shortcut and the startup entry from creating two tray icons."""
    global _instance_mutex
    if os.name != "nt":
        return True
    import ctypes

    _instance_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\SMUAutoEvaluation")
    return ctypes.windll.kernel32.GetLastError() != 183


class TrayApplication:
    def __init__(self, background: bool = False):
        self.settings = Settings.load()
        self.stop_event = threading.Event()
        self.schedule_changed = threading.Event()
        self.run_lock = threading.Lock()
        self.schedule_state = ScheduleState(app_data_dir() / "schedule-state.json")
        # Tk must be used only by the thread that created its root window.  The
        # tray callback can run on another thread, so it communicates with the
        # settings thread through this queue instead of touching Tk directly.
        self._settings_lock = threading.Lock()
        self._settings_thread: threading.Thread | None = None
        self._settings_commands: Queue[str] | None = None
        self.last_status = "等待运行"
        self.icon = pystray.Icon(APP_NAME, self._make_icon(), APP_NAME, self._menu())
        self.background = background

    @staticmethod
    def _make_icon() -> Image.Image:
        image = Image.new("RGBA", (64, 64), "#2563eb")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((5, 5, 59, 59), radius=12, fill="#2563eb")
        draw.text((16, 15), "SM", fill="white")
        return image

    def _menu(self):
        return pystray.Menu(
            pystray.MenuItem(lambda _: self.last_status, None, enabled=False),
            pystray.MenuItem("立即运行", lambda *_: self.run_now()),
            pystray.MenuItem("设置", lambda *_: self.open_settings()),
            pystray.MenuItem("打开日志", lambda *_: self.open_log()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda *_: self.quit()),
        )

    def notify(self, message: str, title: str = APP_NAME) -> None:
        try:
            self.icon.notify(message, title)
        except Exception:
            logging.info("通知不可用: %s", message)

    def run_now(self) -> None:
        if self.run_lock.locked():
            self.notify("评课任务正在运行，请稍候。")
            return
        threading.Thread(target=self._execute, daemon=True).start()

    def _run_scheduled(self, slot: ScheduledRun) -> None:
        threading.Thread(target=self._execute, args=(slot,), daemon=True).start()

    def _execute(self, scheduled_run: ScheduledRun | None = None) -> None:
        with self.run_lock:
            self.last_status = "正在运行…"
            self.icon.update_menu()
            try:
                current = Settings.load()
                current.validate()
                count = run_evaluation(current.account, current.password)
                if scheduled_run:
                    self.schedule_state.finish(scheduled_run, True, datetime.now())
                self.last_status = f"上次运行成功：{datetime.now():%m-%d %H:%M}"
                self.notify(f"任务完成，共处理 {count} 门待评课程。")
            except Exception as exc:
                logging.exception("自动评课失败")
                if scheduled_run:
                    self.schedule_state.finish(scheduled_run, False, datetime.now())
                self.last_status = f"运行失败：{datetime.now():%m-%d %H:%M}"
                self.notify(f"运行失败：{exc}")
            finally:
                self.icon.update_menu()
                if scheduled_run:
                    self.schedule_changed.set()

    def _schedule_loop(self) -> None:
        while not self.stop_event.is_set():
            settings = Settings.load()
            try:
                now = datetime.now()
                scheduled_run = self.schedule_state.claim_due_run(now, settings.run_time)
                if scheduled_run:
                    self._run_scheduled(scheduled_run)
                    continue
                target = self.schedule_state.next_wakeup(now, settings.run_time)
                wait_seconds = schedule_wait_seconds(now, target)
            except ValueError:
                wait_seconds = 60
            if self.schedule_changed.wait(wait_seconds):
                self.schedule_changed.clear()
                continue

    def open_settings(self, first_run: bool = False) -> None:
        with self._settings_lock:
            if self._settings_thread and self._settings_thread.is_alive():
                assert self._settings_commands is not None
                self._settings_commands.put("show")
                return

            commands: Queue[str] = Queue()
            thread = threading.Thread(
                target=self._settings_window,
                args=(first_run, commands),
                daemon=True,
                name="settings-window",
            )
            self._settings_commands = commands
            self._settings_thread = thread
        try:
            thread.start()
        except Exception:
            with self._settings_lock:
                if self._settings_thread is thread:
                    self._settings_thread = None
                    self._settings_commands = None
            raise

    def _settings_window(self, first_run: bool, commands: Queue[str]) -> None:
        import tkinter as tk
        from tkinter import messagebox, ttk

        root = None
        try:
            settings = Settings.load()
            root = tk.Tk()
            root.title(f"{APP_NAME} - 设置")
            root.resizable(False, False)
            root.attributes("-topmost", True)
            frame = ttk.Frame(root, padding=18)
            frame.grid()
            values = [
                tk.StringVar(master=root, value=settings.account),
                tk.StringVar(master=root, value=settings.password),
                tk.StringVar(master=root, value=settings.run_time),
            ]
            for row, label in enumerate(("账号", "密码", "每日运行时间")):
                ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=6)
                ttk.Entry(frame, textvariable=values[row], width=30, show="*" if row == 1 else "").grid(row=row, column=1, pady=6)
            startup = tk.BooleanVar(master=root, value=settings.run_at_startup)
            ttk.Checkbutton(frame, text="开机后自动在托盘运行", variable=startup).grid(row=3, columnspan=2, sticky="w", pady=8)

            def save():
                updated = Settings(values[0].get(), values[1].get(), values[2].get(), startup.get())
                try:
                    updated.save()
                    set_startup(updated.run_at_startup)
                except Exception as exc:
                    messagebox.showerror("无法保存", str(exc), parent=root)
                    return
                self.settings = updated
                self.schedule_changed.set()
                messagebox.showinfo("保存成功", "设置已保存。程序会继续在系统托盘后台运行。", parent=root)
                root.destroy()

            ttk.Button(frame, text="保存", command=save).grid(row=4, column=1, sticky="e", pady=(10, 0))

            def process_commands() -> None:
                try:
                    while True:
                        command = commands.get_nowait()
                        if command == "close":
                            root.destroy()
                            return
                        if command == "show":
                            root.deiconify()
                            root.lift()
                            root.focus_force()
                except Empty:
                    pass
                root.after(50, process_commands)

            root.after(50, process_commands)
            root.mainloop()
        except Exception:
            logging.exception("设置窗口异常")
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass
        finally:
            with self._settings_lock:
                if self._settings_commands is commands:
                    self._settings_thread = None
                    self._settings_commands = None

    def open_log(self) -> None:
        import os
        import subprocess

        log = app_data_dir() / "evaluation.log"
        log.touch(exist_ok=True)
        if os.name == "nt":
            os.startfile(log)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(log)])

    def quit(self) -> None:
        self.stop_event.set()
        self.schedule_changed.set()
        with self._settings_lock:
            settings_thread = self._settings_thread
            commands = self._settings_commands
        if settings_thread and settings_thread.is_alive() and commands:
            commands.put("close")
            if settings_thread is not threading.current_thread():
                settings_thread.join(timeout=2)
        self.icon.stop()

    def run(self) -> None:
        threading.Thread(target=self._schedule_loop, daemon=True).start()
        if not self.settings.account:
            def first_run_setup(icon):
                icon.visible = True
                self.open_settings(True)

            self.icon.run(setup=first_run_setup)
        else:
            self.icon.run()


def configure_logging() -> None:
    directory = app_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=directory / "evaluation.log", format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--background", action="store_true")
    args = parser.parse_args()
    if not acquire_single_instance():
        return
    configure_logging()
    TrayApplication(args.background).run()


if __name__ == "__main__":
    main()
