# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['redsys/server/main.py'],
    pathex=['redsys/server'],
    binaries=[],
    # REDSYS vendor binaries are installed beside the application by Inno
    # Setup.  Bundling the legacy ``redsys/_internal`` directory here embeds
    # another PyInstaller runtime (including base_library.zip) and corrupts
    # this service's own embedded Python environment.
    datas=[],
    hiddenimports=[],
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
    name='redsys_service',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
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
    name='redsys_service',
)
