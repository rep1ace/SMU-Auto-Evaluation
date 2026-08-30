import importlib.util
import sys
import threading
import types
from queue import Queue

import pytest


@pytest.fixture
def tray_app_module(monkeypatch):
    """Import the tray module even in minimal test environments."""
    if importlib.util.find_spec("pystray") is None:
        pystray = types.ModuleType("pystray")
        pystray.Icon = object

        class Menu:
            SEPARATOR = object()

            def __init__(self, *items):
                self.items = items

        pystray.Menu = Menu
        pystray.MenuItem = lambda *args, **kwargs: (args, kwargs)
        monkeypatch.setitem(sys.modules, "pystray", pystray)

    sys.modules.pop("tray_app", None)
    import tray_app

    return tray_app


def make_application(module):
    app = module.TrayApplication.__new__(module.TrayApplication)
    app.settings = None
    app.stop_event = threading.Event()
    app.schedule_changed = threading.Event()
    app._settings_lock = threading.Lock()
    app._settings_thread = None
    app._settings_commands = None
    app.icon = types.SimpleNamespace(stop=lambda: None)
    return app


def test_open_settings_reuses_the_existing_settings_thread(monkeypatch, tray_app_module):
    app = make_application(tray_app_module)
    created = []

    class IdleThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False
            created.append(self)

        def start(self):
            self.started = True

        def is_alive(self):
            return True

    monkeypatch.setattr(tray_app_module.threading, "Thread", IdleThread)

    app.open_settings()
    app.open_settings()

    assert len(created) == 1
    assert created[0].started
    assert app._settings_commands.get_nowait() == "show"


def test_quit_asks_the_settings_thread_to_close_and_waits_for_it(tray_app_module):
    app = make_application(tray_app_module)
    commands = Queue()

    class SettingsThread:
        def __init__(self):
            self.join_timeout = None

        def is_alive(self):
            return True

        def join(self, timeout):
            self.join_timeout = timeout

    settings_thread = SettingsThread()
    stopped = []
    app.icon = types.SimpleNamespace(stop=lambda: stopped.append(True))
    app._settings_thread = settings_thread
    app._settings_commands = commands

    app.quit()

    assert commands.get_nowait() == "close"
    assert settings_thread.join_timeout == 2
    assert app.stop_event.is_set()
    assert app.schedule_changed.is_set()
    assert stopped == [True]


def test_settings_window_binds_values_to_its_root_and_saves_visible_values(monkeypatch, tray_app_module):
    class Variable:
        def __init__(self, *, master, value):
            self.master = master
            self.value = value
            master.variables.append(self)

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class Root:
        instances = []
        on_mainloop = None

        def __init__(self):
            self.variables = []
            self.entries = []
            self.checkbuttons = []
            self.buttons = []
            self.callbacks = []
            self.deiconified = self.lifted = self.focused = self.destroyed = False
            type(self).instances.append(self)

        def title(self, _):
            pass

        def resizable(self, *_):
            pass

        def attributes(self, *_):
            pass

        def after(self, _, callback):
            self.callbacks.append(callback)

        def deiconify(self):
            self.deiconified = True

        def lift(self):
            self.lifted = True

        def focus_force(self):
            self.focused = True

        def destroy(self):
            self.destroyed = True

        def mainloop(self):
            self.callbacks.pop(0)()  # Process the queued "show" request.
            type(self).on_mainloop(self)

    class Frame:
        def __init__(self, root, **_):
            self.root = root

        def grid(self, **_):
            pass

    class Widget:
        def grid(self, **_):
            return self

    class Entry(Widget):
        def __init__(self, frame, *, textvariable, **_):
            self.textvariable = textvariable
            frame.root.entries.append(self)

    class Checkbutton(Widget):
        def __init__(self, frame, *, variable, **_):
            self.variable = variable
            frame.root.checkbuttons.append(self)

    class Button(Widget):
        def __init__(self, frame, *, command, **_):
            self.command = command
            frame.root.buttons.append(self)

    fake_tk = types.ModuleType("tkinter")
    fake_tk.Tk = Root
    fake_tk.StringVar = Variable
    fake_tk.BooleanVar = Variable
    fake_ttk = types.ModuleType("tkinter.ttk")
    fake_ttk.Frame = Frame
    fake_ttk.Label = lambda *args, **kwargs: Widget()
    fake_ttk.Entry = Entry
    fake_ttk.Checkbutton = Checkbutton
    fake_ttk.Button = Button
    fake_messagebox = types.ModuleType("tkinter.messagebox")
    fake_messagebox.showinfo = lambda *args, **kwargs: None
    fake_messagebox.showerror = lambda *args, **kwargs: None
    fake_tk.ttk = fake_ttk
    fake_tk.messagebox = fake_messagebox
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    monkeypatch.setitem(sys.modules, "tkinter.ttk", fake_ttk)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", fake_messagebox)

    original = tray_app_module.Settings("test-account", "test-password", "09:30", True)
    saved = []
    monkeypatch.setattr(tray_app_module.Settings, "load", lambda: original)
    monkeypatch.setattr(tray_app_module.Settings, "save", lambda settings: saved.append(settings))
    monkeypatch.setattr(tray_app_module, "set_startup", lambda enabled: None)

    def edit_and_save(root):
        assert [entry.textvariable.get() for entry in root.entries] == ["test-account", "test-password", "09:30"]
        assert all(variable.master is root for variable in root.variables)
        root.entries[0].textvariable.set("edited-account")
        root.entries[1].textvariable.set("edited-password")
        root.entries[2].textvariable.set("10:45")
        root.checkbuttons[0].variable.set(False)
        root.buttons[0].command()

    Root.on_mainloop = edit_and_save
    app = make_application(tray_app_module)
    commands = Queue()
    commands.put("show")

    app._settings_commands = commands
    app._settings_window(False, commands)

    assert saved == [tray_app_module.Settings("edited-account", "edited-password", "10:45", False)]
    assert app.settings == saved[0]
    assert app.schedule_changed.is_set()
    assert Root.instances[0].deiconified
    assert Root.instances[0].lifted
    assert Root.instances[0].focused
    assert Root.instances[0].destroyed
