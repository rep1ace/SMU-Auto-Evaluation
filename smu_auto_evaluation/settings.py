from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "SMU Auto Evaluation"


def app_data_dir() -> Path:
    import os

    root = os.environ.get("APPDATA") or str(Path.home())
    return Path(root) / APP_NAME


@dataclass
class Settings:
    account: str = ""
    password: str = ""
    run_time: str = "00:10"
    run_at_startup: bool = True

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or app_data_dir() / "config.ini"
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        return cls(
            account=parser.get("login", "account", fallback=""),
            password=parser.get("login", "password", fallback=""),
            run_time=parser.get("schedule", "time", fallback="00:10"),
            run_at_startup=parser.getboolean("general", "run_at_startup", fallback=True),
        )

    def validate(self) -> None:
        if not self.account.strip() or not self.password:
            raise ValueError("请输入统一身份认证账号和密码")
        try:
            hour, minute = (int(part) for part in self.run_time.split(":"))
        except (ValueError, TypeError) as exc:
            raise ValueError("运行时间应为 HH:MM 格式") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("运行时间超出有效范围")

    def save(self, path: Path | None = None) -> Path:
        self.validate()
        path = path or app_data_dir() / "config.ini"
        path.parent.mkdir(parents=True, exist_ok=True)
        parser = configparser.ConfigParser()
        parser["login"] = {"account": self.account.strip(), "password": self.password}
        parser["schedule"] = {"time": self.run_time}
        parser["general"] = {"run_at_startup": str(self.run_at_startup)}
        with path.open("w", encoding="utf-8") as file:
            parser.write(file)
        return path
