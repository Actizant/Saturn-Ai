# -*- mode: python ; coding: utf-8 -*-
import os
cwd = 'D:\\TiTech Prabha Solution\\Saturn\\Saturn\\Saturn-AI---Lite-main\\Saturn-AI---Lite-main'
source_dir = 'D:\\TiTech Prabha Solution\\Saturn\\Saturn\\Saturn-AI---Lite-main\\Saturn-AI---Lite-main\\installer\\dist\\SaturnEcho'

a = Analysis(
    ['D:\\TiTech Prabha Solution\\Saturn\\Saturn\\Saturn-AI---Lite-main\\Saturn-AI---Lite-main\\install_wizard.py'],
    pathex=[],
    binaries=[],
    datas=[
        (source_dir, 'SaturnEcho'),
        (os.path.join(cwd, 'assets'), 'assets')
    ],
    hiddenimports=['PyQt6', 'shutil', 'PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebEngineCore'],
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
    name='SaturnEcho_Setup',
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
    uac_admin=True,
    icon=os.path.join(cwd, 'assets/Saturn_Logo.ico') if __import__('os').path.exists(__import__('os').path.join(cwd, 'assets/Saturn_Logo.ico')) else None,
    version=os.path.join(cwd, 'version_setup.txt')
)
