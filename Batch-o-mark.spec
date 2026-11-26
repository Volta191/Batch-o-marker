# -*- mode: python ; coding: utf-8 -*-
import sys

block_cipher = None

a = Analysis(
    ['desktop_app.py'],
    pathex=[],
    binaries=[],
    datas=[('templates_store', 'templates_store')],
    hiddenimports=['engine'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['fastapi', 'uvicorn', 'starlette', 'aiofiles'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Batch-o-mark',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='Batch-o-mark',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='Batch-o-mark.app',
        icon=None,
        bundle_identifier='com.batchomark.app',
    )
