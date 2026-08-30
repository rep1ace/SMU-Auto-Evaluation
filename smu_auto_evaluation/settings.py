from __future__ import annotations

import base64
import configparser
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "SMU Auto Evaluation"
PASSWORD_ENCODING_OPTION = "password_encoding"
PASSWORD_ENCODING_BASE64_UTF8 = "base64-utf8"


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
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path, encoding="utf-8")

        password = cls._load_password(parser, path)
        return cls(
            account=parser.get("login", "account", fallback=""),
            password=password,
            run_time=parser.get("schedule", "time", fallback="00:10"),
            run_at_startup=parser.getboolean("general", "run_at_startup", fallback=True),
        )

    @staticmethod
    def _load_password(parser: configparser.ConfigParser, path: Path) -> str:
        """Load new encoded credentials and retain the old INI interpretation.

        Configurations created before password encoding used ConfigParser's
        interpolation.  Read those files through the same interpretation so a
        previously escaped percent sign continues to represent the same value.
        """
        encoding = parser.get("login", PASSWORD_ENCODING_OPTION, fallback="")
        raw_password = parser.get("login", "password", fallback="")
        if not encoding:
            legacy_parser = configparser.ConfigParser()
            legacy_parser.read(path, encoding="utf-8")
            try:
                return legacy_parser.get("login", "password", fallback="")
            except (configparser.Error, ValueError) as exc:
                raise ValueError("配置中的密码格式无效") from exc

        if encoding != PASSWORD_ENCODING_BASE64_UTF8:
            raise ValueError("配置中的密码编码不受支持")
        try:
            return base64.b64decode(raw_password.encode("ascii"), validate=True).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError, ValueError) as exc:
            raise ValueError("配置中的密码格式无效") from exc

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
        parser = configparser.ConfigParser(interpolation=None)
        encoded_password = base64.b64encode(self.password.encode("utf-8")).decode("ascii")
        parser["login"] = {
            "account": self.account.strip(),
            "password": encoded_password,
            PASSWORD_ENCODING_OPTION: PASSWORD_ENCODING_BASE64_UTF8,
        }
        parser["schedule"] = {"time": self.run_time}
        parser["general"] = {"run_at_startup": str(self.run_at_startup)}
        with path.open("w", encoding="utf-8") as file:
            parser.write(file)
        return path
