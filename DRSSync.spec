# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['PIL._tkinter_finder', 'PIL.ImageTk', 'PIL.Image', 'pyodbc', 'plyer', 'plyer.platforms.win.notification', 'psycopg2', 'psycopg2.extras', 'psycopg2._psycopg']
hiddenimports += collect_submodules('PIL')
hiddenimports += collect_submodules('psycopg2')


a = Analysis(
    ['D:\\IMC SYNC _TOOLS\\DRS_CLINIC\\DRS_SYNCTOOL\\app.py'],
    pathex=[],
    binaries=[],
    datas=[('D:\\IMC SYNC _TOOLS\\DRS_CLINIC\\DRS_SYNCTOOL\\config.json', '.'), ('D:\\IMC SYNC _TOOLS\\DRS_CLINIC\\DRS_SYNCTOOL\\DRS_icon.ico', '.'), ('D:\\IMC SYNC _TOOLS\\DRS_CLINIC\\DRS_SYNCTOOL\\DRS_icon.png', '.')],
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
    a.binaries,
    a.datas,
    [],
    name='DRSSync',
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
    icon=['D:\\IMC SYNC _TOOLS\\DRS_CLINIC\\DRS_SYNCTOOL\\DRS_icon.ico'],
)
