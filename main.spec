# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_data_files

project_root = os.path.abspath('src')
pvlib_datas = collect_data_files('pvlib', include_py_files=False)

a = Analysis(
    ['src\\main.py'],
    pathex=[project_root],
    binaries=[],
    datas=pvlib_datas + [
        ('src\\resources\\batteries.json',  'resources'),
        ('src\\resources\\inverters.json',  'resources'),
        ('src\\resources\\pv_systems.json', 'resources'),
        ('src\\ui\\main_window.ui',         'ui'),
        ('src\\ui\\pv_generator_page.ui',   'ui'),
        ('src\\icons\\icon.ico',            'icons'),
        ('src\\icons\\splash.png',          'icons'),
    ],
    hiddenimports=[
        'gui.mainwindow',
        'gui.ui_main_window',
        'gui.widgets',
        'logic.calculation',
        'worker.calcworker',
        'bkwsimx.version',
        'cx_Freeze',
        'cx_Logging',
    ],
    hookspath=['hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib.tests'
    ],
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
    name='BKWSimX',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['src\\icons\\icon.ico'],
)
