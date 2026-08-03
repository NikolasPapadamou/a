# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all


curl_datas, curl_binaries, curl_hiddenimports = collect_all("curl_cffi")

a = Analysis(
    ["RegulatoryMonitorApp.py"],
    pathex=[],
    binaries=curl_binaries,
    datas=curl_datas,
    hiddenimports=(
        curl_hiddenimports
        + [
            "certifi",
            "cffi",
            "pythoncom",
            "pywintypes",
            "win32com.client",
        ]
    ),
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
    [],
    exclude_binaries=True,
    name="RegulatoryMonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="RegulatoryMonitor",
)
