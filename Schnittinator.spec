# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller-Spec fuer Schnittinator.
# Diese Datei liegt im selben Ordner wie bohrtool_gui.py und wird gebaut mit:
#     pyinstaller Schnittinator.spec
#
# WICHTIG: Muss unter Windows mit einem Windows-Python ausgefuehrt werden,
# damit eine Windows-.exe entsteht (PyInstaller baut immer nur fuer das
# Betriebssystem, auf dem es laeuft - kein Cross-Compiling).

from PyInstaller.utils.hooks import collect_all

datas = [('assets', 'assets')]
binaries = []
hiddenimports = ['PIL._tkinter_finder']

# OCP (OpenCascade-Bindings) bringt viele native Bibliotheken und
# Datendateien mit, die PyInstaller sonst nicht automatisch findet.
ocp_datas, ocp_binaries, ocp_hiddenimports = collect_all('OCP')
datas += ocp_datas
binaries += ocp_binaries
hiddenimports += ocp_hiddenimports

block_cipher = None

a = Analysis(
    ['bohrtool_gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Schnittinator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # kein Konsolenfenster im Hintergrund
    icon='assets/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Schnittinator',
)
