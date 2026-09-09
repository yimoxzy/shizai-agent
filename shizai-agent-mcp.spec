# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['release\\windows_mcp_entry.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The MCP server does not use multiprocessing.  PyInstaller can pull its
    # runtime hook in through AnyIO's optional dependency graph; in a one-file
    # stdio process that hook races interpreter shutdown and writes a closed
    # stderr stream.  Excluding the unused package keeps shutdown quiet.
    excludes=['multiprocessing'],
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
    name='shizai-agent-mcp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
