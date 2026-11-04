# -*- mode: python ; coding: utf-8 -*-
import os

cwd = os.path.abspath(os.getcwd())

a = Analysis(
    [os.path.join(cwd, 'installer', 'uninstall_wizard.py')],
    pathex=[],
    binaries=[],
    datas=[
        (os.path.join(cwd, 'assets'), 'assets')
    ] if os.path.exists(os.path.join(cwd, 'assets')) else [],
    hiddenimports=['PyQt6', 'win32com.client', 'winreg'],
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
    name='Uninstall',
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
    icon=os.path.join(cwd, 'assets', 'Saturn_Logo.ico') if os.path.exists(os.path.join(cwd, 'assets', 'Saturn_Logo.ico')) else None,
)
