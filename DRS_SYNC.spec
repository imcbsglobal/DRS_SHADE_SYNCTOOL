# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['PIL._tkinter_finder', 'PIL.ImageTk', 'PIL.Image', 'pystray', 'pystray._win32']
hiddenimports += collect_submodules('PIL')
hiddenimports += collect_submodules('pystray')


a = Analysis(
    ['D:\\IMC SYNC _TOOLS\\DRS_CLINIC\\DRS_SYNCTOOL\\app.py'],
    pathex=[],
    binaries=[],
    datas=[('D:\\IMC SYNC _TOOLS\\DRS_CLINIC\\DRS_SYNCTOOL\\config.json', '.'), ('D:\\IMC SYNC _TOOLS\\DRS_CLINIC\\DRS_SYNCTOOL\\sync.py', '.'), ('D:\\IMC SYNC _TOOLS\\DRS_CLINIC\\DRS_SYNCTOOL\\DRS_icon.ico', '.'), ('D:\\IMC SYNC _TOOLS\\DRS_CLINIC\\DRS_SYNCTOOL\\DRS_icon.png', '.')],
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
    name='DRS_SYNC',
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
    version='D:\\IMC SYNC _TOOLS\\DRS_CLINIC\\DRS_SYNCTOOL\\version_info.txt',
    icon=['D:\\IMC SYNC _TOOLS\\DRS_CLINIC\\DRS_SYNCTOOL\\DRS_icon.ico'],
    manifest='D:\\IMC SYNC _TOOLS\\DRS_CLINIC\\DRS_SYNCTOOL\\DRS_SYNC.manifest',
)
