# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for DTS to AC-3 Converter."""

a = Analysis(
    ['converter.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['tkinter'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludedimports=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='DTS to AC-3 Converter',
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
    name='DTS to AC-3 Converter.app',
    icon='icons/converter_icon.icns',  # macOS icon
    bundle_identifier='com.simonwoollams.dtstoac3',
    info_plist={
        'NSPrincipalClass': 'NSApplication',
        'NSHighResolutionCapable': 'True',
    },
)
