"""PyInstaller 打包入口。

为什么不直接把 `clicktrans/__main__.py` 丢给 Analysis：
`__main__.py` 里用的是相对导入（`from . import APP_NAME`），它作为**模块**
被 `python -m clicktrans` 执行时没问题，但被 PyInstaller 当**脚本**执行时
没有包上下文，会直接 ImportError。

这里用绝对导入把它包一层，顺便这也是 `pyproject.toml` 里
`[project.gui-scripts]` 声明的入口，两边保持一致。
"""

from __future__ import annotations

import sys

from clicktrans.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
