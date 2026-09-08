# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：字幕翻译工具
#
# 构建：pyinstaller subtitle_translator.spec
# 产物：dist/字幕翻译工具.exe（单文件、无控制台窗口）

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('resources', 'resources')],   # 术语库 / 预设术语 / QSS 主题
    hiddenimports=[
        'darkdetect._windows_detect',      # Windows 明暗主题检测（平台特定子模块）
        'tiktoken_ext.openai_public',      # tiktoken 编码数据加载器
        'ctranslate2',                     # faster-whisper 推理后端（字幕提取）
        'av',                              # PyAV：视频/音频解码（随 faster-whisper）
        'tokenizers',                      # huggingface_hub 模型下载依赖
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='字幕翻译工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI 应用，不弹控制台
    icon=None,
)
