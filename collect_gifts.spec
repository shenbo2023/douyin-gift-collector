# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置（在 Windows 上执行 build.bat）

import sys
from pathlib import Path

root = Path(SPECPATH)

a = Analysis(
    [str(root / "app_gui.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / "config.json"), "."),
        (str(root / "proto" / "douyin_live_pb2.py"), "proto"),
        (str(root / "sign.js"), "."),
    ],
    hiddenimports=[
        "google.protobuf",
        "google.protobuf.internal",
        "google.protobuf.internal.builder",
        "google.protobuf.descriptor",
        "google.protobuf.descriptor_pool",
        "google.protobuf.symbol_database",
        "google.protobuf.pyext",
        "douyin_live_pb2",
        "certifi",
        "collect_gifts",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="抖音礼物采集",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
