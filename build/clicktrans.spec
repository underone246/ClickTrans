# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（单文件、无控制台）。

    python -m PyInstaller build/clicktrans.spec --noconfirm
    产物：dist/ClickTrans.exe

这个 spec 里每一段 collect 都是有原因的，注释写清楚，免得以后被"优化"掉。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# spec 文件在 build/ 下，所以项目根是它的上一级
ROOT = Path(SPECPATH).resolve().parent  # noqa: F821  (SPECPATH 由 PyInstaller 注入)

datas = []
binaries = []
hiddenimports = []

# --------------------------------------------------------------------------- #
# RapidOCR：必须带上包内的 onnx 权重和 config.yaml
# --------------------------------------------------------------------------- #
# 它把模型放在 rapidocr_onnxruntime/models/*.onnx，把参数放在各级 config.yaml。
# PyInstaller 只收 .py，这些文件不会自动带上——漏了的话打包能成功，但一运行
# 就报「模型路径不存在」，而且只在真正框选时才炸，非常难查。
datas += collect_data_files("rapidocr_onnxruntime")
# ch_ppocr_v3_det / v3_rec / v2_cls 三个子包是按需 import 的，全部收进来
hiddenimports += collect_submodules("rapidocr_onnxruntime")

# --------------------------------------------------------------------------- #
# onnxruntime
# --------------------------------------------------------------------------- #
# 不用 collect_submodules：那会把 onnxruntime.tools.* （量化、转换脚本）也拖进来，
# 徒增体积还会引入不必要的依赖。只显式声明真正会被加载的 capi。
hiddenimports += [
    "onnxruntime",
    "onnxruntime.capi",
    "onnxruntime.capi._pybind_state",
    "onnxruntime.capi.onnxruntime_pybind11_state",
    "onnxruntime.capi.onnxruntime_validation",
]

# --------------------------------------------------------------------------- #
# pywin32：DPAPI 加密 api key + 注册表自启
# --------------------------------------------------------------------------- #
# win32crypt 是 .pyd，静态分析看不见（代码里写在函数内 try/except 里）
hiddenimports += ["win32crypt", "win32api", "win32con", "win32gui", "pywintypes"]

# --------------------------------------------------------------------------- #
# PIL：**不要排除**
# --------------------------------------------------------------------------- #
# rapidocr_onnxruntime/utils.py 里有 `from PIL import Image`。
# 看着像"只有测试用"，实际是运行依赖，排掉就启动即崩。

# --------------------------------------------------------------------------- #
# 排除项
# --------------------------------------------------------------------------- #
excludes = [
    # Qt 里我们用不到的大块头，每个都是几十 MB
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtWebChannel",
    "PyQt6.QtQml",
    "PyQt6.QtQuick",
    "PyQt6.QtQuick3D",
    "PyQt6.QtQuickWidgets",
    "PyQt6.QtMultimedia",
    "PyQt6.QtMultimediaWidgets",
    "PyQt6.Qt3DCore",
    "PyQt6.Qt3DRender",
    "PyQt6.QtCharts",
    "PyQt6.QtDataVisualization",
    "PyQt6.QtBluetooth",
    "PyQt6.QtNfc",
    "PyQt6.QtPositioning",
    "PyQt6.QtSensors",
    "PyQt6.QtSerialPort",
    "PyQt6.QtSql",
    "PyQt6.QtTest",
    "PyQt6.QtDesigner",
    "PyQt6.QtHelp",
    "PyQt6.QtPdf",
    "PyQt6.QtPdfWidgets",
    "PyQt6.QtSpatialAudio",
    "PyQt6.QtRemoteObjects",
    "PyQt6.QtScxml",
    "PyQt6.QtStateMachine",
    "PyQt6.QtTextToSpeech",
    "PyQt6.QtWebSockets",
    # 科学计算 / 开发期依赖，运行时不碰
    "matplotlib",
    "scipy",
    "pandas",
    "IPython",
    "jupyter",
    "notebook",
    "pytest",
    "tkinter",
    "_tkinter",
    "test",
    "unittest",
    "pydoc",
]

a = Analysis(
    [str(ROOT / "build" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ClickTrans",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX 会破坏 onnxruntime / Qt 的 DLL，必须关
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # 托盘程序，绝不能弹控制台（也让 log.py 走「无 stdout 时跳过 console handler」分支）
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
