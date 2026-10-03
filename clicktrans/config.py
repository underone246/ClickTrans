"""配置中心：TOML 读写 + API Key 的 DPAPI 加密存储。

配置是给人手改的，所以：
- 读取用标准库 tomllib
- 写回用一个极简的 TOML 序列化器（够用即可，避免为一个写操作引依赖）
- API Key 绝不写进 config.toml，单独走 DPAPI 加密文件
"""

from __future__ import annotations

import base64
import os
import sys
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from .lang import Lang, parse_lang

APP_DIR = Path(os.environ.get("CLICKTRANS_HOME", Path.home() / ".clicktrans"))


def app_dir() -> Path:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    return APP_DIR


def config_path() -> Path:
    return app_dir() / "config.toml"


def secret_path() -> Path:
    return app_dir() / "secret.bin"


def log_dir() -> Path:
    return app_dir() / "logs"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class GeneralConfig:
    hotkey: str = "alt+q"
    preferred_lang: str = "zh"
    launch_at_startup: bool = False
    single_instance: bool = True
    log_level: str = "INFO"

    @property
    def preferred(self) -> Lang:
        return parse_lang(self.preferred_lang, Lang.ZH)


@dataclass
class OcrConfig:
    engine: str = "rapidocr"
    model: str = "mobile"  # mobile = 包内自带(PP-OCRv3 中英) | server = 自备模型目录
    # 默认 1.0：检测器内部会把短边放大到 limit_side_len，我们再放大是重复劳动
    upscale: float = 1.0
    min_confidence: float = 0.6
    unload_after_idle_min: int = 30
    max_pixels: int = 4_000_000  # 超过这个像素数先缩放，避免超大选区拖慢推理
    # 限制长边而不是放大短边。库默认是 min/736，会把宽扁的文字带放大好几倍
    det_limit_side_len: int = 960
    det_limit_type: str = "max"
    # 屏幕上的文字几乎不会是倒着的，方向分类纯属浪费
    use_angle_cls: bool = False
    rec_batch_num: int = 6


@dataclass
class TranslateConfig:
    provider: str = "openai_compatible"
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    stream: bool = True
    timeout_s: int = 20
    proxy: str = ""
    ja_style: str = "polite"
    temperature: float = 0.2


@dataclass
class HistoryConfig:
    path: str = ""  # 留空则用 ~/.clicktrans/history.json
    limit: int = 500
    dedupe: bool = True

    def resolved_path(self) -> Path:
        if self.path:
            return Path(os.path.expanduser(self.path))
        return app_dir() / "history.json"


@dataclass
class UiConfig:
    font_size: int = 15
    card_width: int = 420
    card_opacity: float = 0.96
    auto_hide_ms: int = 0
    theme: str = "follow_system"


@dataclass
class GlossaryConfig:
    terms: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    translate: TranslateConfig = field(default_factory=TranslateConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    glossary: GlossaryConfig = field(default_factory=GlossaryConfig)


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #


def _fill(obj: Any, data: dict) -> Any:
    """把 dict 里的已知字段填进 dataclass，忽略未知键（方便向前兼容）。"""
    if not isinstance(data, dict):
        return obj
    known = {f.name for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            continue
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _fill(current, value)
        elif isinstance(current, bool) and isinstance(value, bool):
            setattr(obj, key, value)
        elif isinstance(current, (int, float)) and isinstance(value, (int, float)) and not isinstance(value, bool):
            setattr(obj, key, type(current)(value))
        elif isinstance(current, str) and isinstance(value, str):
            setattr(obj, key, value)
        elif isinstance(current, dict) and isinstance(value, dict):
            setattr(obj, key, {str(k): str(v) for k, v in value.items()})
        else:
            setattr(obj, key, value)
    return obj


def load() -> Config:
    cfg = Config()
    path = config_path()
    if path.exists():
        try:
            with path.open("rb") as fh:
                _fill(cfg, tomllib.load(fh))
        except Exception:
            # 配置坏了不能让程序起不来，用默认值继续
            pass
    return cfg


# --------------------------------------------------------------------------- #
# 写回（极简 TOML 序列化）
# --------------------------------------------------------------------------- #


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise TypeError(f"不支持的 TOML 值类型: {type(value)!r}")


def dump(cfg: Config) -> str:
    data = asdict(cfg)
    lines: list[str] = ["# ClickTrans 配置。改完重启生效。", ""]
    for section, payload in data.items():
        if not isinstance(payload, dict):
            continue
        scalars = {k: v for k, v in payload.items() if not isinstance(v, dict)}
        nested = {k: v for k, v in payload.items() if isinstance(v, dict)}

        if scalars:
            lines.append(f"[{section}]")
            for key, value in scalars.items():
                lines.append(f"{key} = {_toml_scalar(value)}")
            lines.append("")

        for sub, sub_payload in nested.items():
            lines.append(f"[{section}.{sub}]")
            if not sub_payload:
                lines.append("# （空）")
            for key, value in sub_payload.items():
                lines.append(f"{key} = {_toml_scalar(value)}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save(cfg: Config) -> None:
    path = config_path()
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(dump(cfg), encoding="utf-8")
    os.replace(tmp, path)


def ensure_default_config() -> Config:
    """首次运行时落一份默认配置，方便用户直接手改。"""
    cfg = load()
    if not config_path().exists():
        try:
            save(cfg)
        except Exception:
            pass
    return cfg


# --------------------------------------------------------------------------- #
# API Key：Windows DPAPI 按当前用户加密
# --------------------------------------------------------------------------- #

_PLAIN_PREFIX = b"PLAIN:"


def save_api_key(key: str) -> str:
    """保存 Key，返回实际使用的存储方式（'dpapi' 或 'plain'）。"""
    raw = key.encode("utf-8")
    blob = None
    method = "plain"
    if sys.platform == "win32":
        try:
            import win32crypt  # type: ignore

            blob = win32crypt.CryptProtectData(raw, None, None, None, None, 0)
            method = "dpapi"
        except Exception:
            blob = None
    if blob is None:
        blob = _PLAIN_PREFIX + base64.b64encode(raw)

    path = secret_path()
    tmp = path.with_suffix(".bin.tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, path)
    return method


def load_api_key() -> str:
    # 环境变量优先级最高，方便临时测试和 CI
    env = os.environ.get("CLICKTRANS_API_KEY")
    if env:
        return env

    path = secret_path()
    if not path.exists():
        return ""
    blob = path.read_bytes()
    if blob.startswith(_PLAIN_PREFIX):
        return base64.b64decode(blob[len(_PLAIN_PREFIX):]).decode("utf-8", "replace")
    if sys.platform == "win32":
        try:
            import win32crypt  # type: ignore

            return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1].decode("utf-8")
        except Exception:
            return ""
    return ""


def has_api_key() -> bool:
    return bool(load_api_key())
