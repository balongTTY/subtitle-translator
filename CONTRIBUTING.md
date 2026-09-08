# 贡献指南

感谢你有兴趣为这个项目做贡献！本文档说明如何搭建环境、提交代码和报告问题。

## 目录

- [行为准则](#行为准则)
- [报告问题](#报告问题)
- [开发环境](#开发环境)
- [项目结构](#项目结构)
- [开发规范](#开发规范)
- [测试要求](#测试要求)
- [提交与 PR](#提交与-pr)
- [可以做什么](#可以做什么)

---

## 行为准则

参与本项目即表示你同意遵守 [行为准则](CODE_OF_CONDUCT.md)。请保持友善与尊重。

## 报告问题

提 Issue 前请先搜索是否已有相同问题。提 Issue 时请尽量包含：

- **复现步骤**：从启动到出错的完整操作
- **期望行为 vs 实际行为**
- **环境**：Windows 版本、Python 版本、`pip list | grep -E "PyQt5|pysubs2|openai"`
- **日志**：`%APPDATA%\SubtitleTranslator\logs\translator.log` 的相关片段
- **样本**：能复现的最小字幕文件（脱敏后）

> ⚠️ **请勿在 Issue 中粘贴 API Key**。日志里也不会记录密钥，但请自行确认。

## 开发环境

```bash
git clone https://github.com/balongTTY/subtitle-translator.git
cd subtitle-translator
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"          # 含 pytest / pyinstaller
python main.py                   # 启动
```

要求 Python ≥ 3.10。主要开发/测试平台是 Windows 10/11。

## 项目结构

```
字幕工具/
├── main.py                  入口
├── config/settings.py       QSettings 封装 + 密钥存储（DPAPI）
├── core/                    核心流水线
│   ├── translator.py        编排器（多 worker 并行 + 信号聚合）
│   ├── analyzer.py          全篇分析（提示词 + JSON 解析）
│   ├── chunker.py           分块（token 预算 + overlap + 话题边界）
│   ├── qc_checker.py        质检规则
│   ├── merger.py            合并回写
│   ├── tag_handler.py       ASS/SRT 标签保护
│   ├── subtitle_io.py       字幕读写（编码探测）
│   ├── corpus_manager.py    语料库 + 预设术语
│   ├── extractor.py         字幕提取（faster-whisper）
│   ├── asr_online.py        在线 ASR 引擎
│   ├── audio_splitter.py    音频切块
│   └── model_download.py    模型下载
├── services/                LLM 服务层
│   ├── base_llm.py          抽象接口
│   ├── openai_service.py    OpenAI / DeepSeek / 自定义
│   ├── claude_service.py    Claude（含 Prompt Caching）
│   ├── prompt_builder.py    翻译提示词 + 响应解析
│   └── rate_limiter.py      共享令牌桶限流
├── gui/                     PyQt5 界面
├── utils/                   常量 / 日志 / token 估算
├── resources/               语料库 / 预设术语 / QSS 主题
├── tests/                   pytest 单元测试
│   └── e2e/                 端到端验证脚本（fake LLM，不触网）
└── docs/                    文档与修复记录
```

## 开发规范

- **类型注解**：公开函数与方法请标注类型（项目使用 `X | None` 语法，需 Python ≥ 3.10）
- **日志**：用 `logging.getLogger("subtitle_translator")`，不要 `print`
- **线程**：GUI 操作只能在主线程；耗时任务走 `QThread` / `QThreadPool`，用 Qt 信号回传
- **注释**：只写代码本身看不出的约束（为什么这样写），不要复述代码在做什么
- **异常**：不要吞掉异常后静默继续——至少记日志；涉及用户数据丢失的路径必须显式标记状态
- **格式**：4 空格缩进，行宽 100（见 `.editorconfig` / `pyproject.toml` 的 ruff 配置）

```bash
ruff check .        # 静态检查
ruff format .       # 格式化（可选）
```

## 测试要求

项目有两层测试，**提交前请确保都通过**：

```bash
# 1) 单元测试（pytest，149 项）
pytest

# 2) 端到端验证（脚本式，使用 fake LLM，不消耗 API 额度）
python tests/e2e/run_e2e.py
```

- **改核心逻辑**（chunker / qc / parser / merger / tag_handler）→ 在 `tests/` 补 pytest 用例
- **改编排/线程/并行**→ 在 `tests/e2e/` 补端到端脚本
- **改 GUI**→ 至少跑通 `tests/e2e/test_parallel_gui.py` 的离屏冒烟

> 端到端脚本会读写本机真实 QSettings 与语料库文件，建议在干净环境运行。

## 提交与 PR

**提交信息**采用约定式前缀：

```
feat(scope): 新功能
fix(scope): 修复
test: 测试
docs: 文档
refactor(scope): 重构
perf(scope): 性能
chore: 杂项
```

scope 用模块名，如 `feat(chunker): 支持按场景间隔断批`。

**PR 要求**：

1. 从 `master` 开分支，一个 PR 只做一件事
2. 描述清楚：**改了什么、为什么改、怎么验证的**
3. 附上测试结果（`pytest` 与 `run_e2e.py` 的输出）
4. 如果改动了用户可见行为，同步更新 `README.md` 与 `CHANGELOG.md`

## 可以做什么

| 方向 | 说明 | 难度 |
|------|------|------|
| 🐛 修 Bug | 看 [Issues](https://github.com/balongTTY/subtitle-translator/issues) | ★☆☆ |
| 🗂️ 术语库扩充 | 往 `resources/corpus_vtuber.json` 或 `glossary_vtuber.json` 添加术语 | ★☆☆ |
| 🌐 多语言界面 | 目前 UI 仅中文，可做 i18n | ★★☆ |
| 🎯 提示词优化 | 改善翻译腔消除效果，需要真实样本对比 | ★★☆ |
| 🧪 测试覆盖 | 补单元测试与端到端场景 | ★★☆ |
| 📦 打包脚本 | 完善 PyInstaller / CI 自动发布 | ★★★ |
| 🎬 更多字幕格式 | 如 TTML、SCC 支持 | ★★★ |

开始做之前，建议先开 Issue 讨论，避免重复劳动。

---

再次感谢你的贡献！
