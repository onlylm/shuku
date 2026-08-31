# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from pathlib import Path

datas = []
binaries = []
hiddenimports = ['keyring.backends.Windows']
tmp_ret = collect_all('defusedxml')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['scripts/organizer_entry.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Qt 6.11 使用 Windows 自带的无版本后缀 ICU ABI。构建宿主的另一份
# ICU 78 同名 DLL 会误入包并遮蔽系统库，引发 QtWidgets 加载失败。
# 不分发/复制系统 DLL；本机 Windows 10 build 19045 从 System32 提供此依赖。
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() != 'icuuc.dll' and not Path(entry[0]).name.lower().startswith('icudt')]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='EbookOrganizer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
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
    name='EbookOrganizer',
)
