"""日志。打包成 --noconsole 之后 print 会抛异常，所有输出必须走 logging。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False


def setup(level: str = "INFO", log_dir: Path | None = None) -> None:
    global _configured
    if _configured:
        return

    root = logging.getLogger("clicktrans")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 控制台：没有 stdout（--noconsole）时静默跳过
    try:
        if sys.stdout is not None and sys.stdout.fileno() >= 0:
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(fmt)
            root.addHandler(sh)
    except Exception:
        pass

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                log_dir / "clicktrans.log",
                maxBytes=1_000_000,
                backupCount=3,
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except Exception:
            pass

    _configured = True


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"clicktrans.{name}")
