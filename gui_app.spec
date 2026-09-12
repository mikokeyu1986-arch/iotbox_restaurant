# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['pystray._win32', 'webview']
hiddenimports += collect_submodules('pystray')


a = Analysis(
    ['D:/iotbox/iot_box_restaurant/gui_app.py'],
    pathex=[],
    binaries=[],
    datas=[('D:/iotbox/iot_box_restaurant/web', 'web'), ('D:/iotbox/iot_box_restaurant/certs', 'certs'), ('D:/iotbox/iot_box_restaurant/runtime_config.json', '.')],
    hiddenimports=hiddenimports,
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
    name='gui_app',
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
    icon=['D:/iotbox/iot_box_restaurant/assets/iotbox-icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='gui_app',
)
