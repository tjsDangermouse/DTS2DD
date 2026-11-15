# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for DTS to AC-3 Converter.

Windows: a one-folder build (dist/DTS2DD/) that the Inno Setup installer
packages together with ffmpeg/ and librempeg/ folders. One-folder starts
quickly (nothing is unpacked to %TEMP% on each launch) and triggers fewer
antivirus false positives than a single-file exe.

macOS: the original single-file app bundle. Set DTS2DD_ONEDIR=1 to build
the Windows-style folder layout on any platform (used for testing).
"""
import os
import sys

APP_NAME = 'DTS to AC-3 Converter'
ONEDIR = sys.platform == 'win32' or bool(os.environ.get('DTS2DD_ONEDIR'))

a = Analysis(
    ['converter.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['tkinter'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest'],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

if ONEDIR:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,  # No console window
        disable_windowed_traceback=False,
        icon='icons/converter_icon.ico',
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name='DTS2DD',
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,  # No console window
        disable_windowed_traceback=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon='icons/converter_icon.ico',  # Windows icon
    )

    app = BUNDLE(
        exe,
        name=f'{APP_NAME}.app',
        icon='icons/converter_icon.icns',  # macOS icon
        bundle_identifier='com.simonwoollams.dtstoac3',
        info_plist={
            'NSPrincipalClass': 'NSApplication',
            'NSHighResolutionCapable': 'True',
        },
    )
