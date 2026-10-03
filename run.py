#!/usr/bin/env python
"""开发期启动脚本。

    python run.py

打包后的入口是 clicktrans.__main__:main。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from clicktrans.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
