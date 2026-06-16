# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置（在 Windows 上执行 build.bat）

import sys
from pathlib import Path

root = Path(SPECPATH)

# HTTPS 推送（backend_push.http_url）在 exe 内需要证书
try:
    import certifi

    certifi_data = [(certifi.where(), "certifi")]
except ImportError:
    certifi_data = []

a = Analysis(
    [str(root / "app_gui.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / "config.example.json"), "."),
        (str(root / "proto" / "douyin_live_pb2.py"), "proto"),
        (str(root / "sign.js"), "."),
        *certifi_data,
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
        "backend_push",
        "requests",
        "urllib3",
        "idna",
        "charset_normalizer",
        "websocket",
        "websocket._abnf",
        "websocket._app",
        "websocket._cookiejar",
        "websocket._core",
        "websocket._exceptions",
        "websocket._handshake",
        "websocket._http",
        "websocket._logging",
        "websocket._socket",
        "websocket._ssl_compat",
        "websocket._url",
        "websocket._utils",
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
