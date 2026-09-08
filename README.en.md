# Subtitle Translator（字幕翻译工具）

> LLM-powered automatic subtitle translation for vtuber / live-stream content — drag in an SRT/ASS/VTT file and fan-sub (烤肉, literally "roasting") in one click.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![PyQt5](https://img.shields.io/badge/GUI-PyQt5-green)](https://pypi.org/project/PyQt5/)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)
[![Tests](https://github.com/balongTTY/subtitle-translator/actions/workflows/tests.yml/badge.svg)](https://github.com/balongTTY/subtitle-translator/actions/workflows/tests.yml)

---

## Why You Need This Tool

If you fan-sub (烤肉, literally "roasting") archived vtuber streams, you know these pains:

1. **Inconsistent terminology** — the same 配信 (stream) can become 直播 in the first five minutes and 放送 in the next five, leaving viewers confused
2. **Heavy translationese** — 「我觉得今天真的很开心呢」「请大家多多指教的说」 — any native Chinese speaker can tell it was machine-translated at a glance
3. **LLMs don't know community conventions** — the fan community calls スパチャ "SC", but the LLM insists on 超级留言 or 醒目留言 (super message)
4. **Manual subtitling is exhausting** — even with a subtitle file in hand, you still translate line by line, hundreds of lines until your eyes blur
5. **Broken context** — the LLM only sees 20 lines at a time and has no idea whether this segment is a boss fight or the streamer reading comments

**This tool solves all of the above.**

---

## Table of Contents

1. [What It Can Do](#what-it-can-do)
2. [Quick Start](#quick-start)
3. [Illustrated Tutorial: Your First Translation](#illustrated-tutorial-your-first-translation)
4. [The Translation Engine: Three-Stage Pipeline](#the-translation-engine-three-stage-pipeline)
5. [Core Concept: The Terminology Database (Corpus)](#core-concept-the-terminology-database-corpus)
6. [Prompt Engineering: Why It Translates So Well](#prompt-engineering-why-it-translates-so-well)
7. [Supported LLMs and Configuration](#supported-llms-and-configuration)
8. [Settings Reference](#settings-reference)
9. [Project Architecture](#project-architecture)
10. [Data Flow in Detail](#data-flow-in-detail)
11. [Developer Guide](#developer-guide)
12. [Testing](#testing)
13. [Performance and Cost](#performance-and-cost)
14. [Troubleshooting](#troubleshooting)
15. [Packaging and Distribution](#packaging-and-distribution)
16. [FAQ](#faq)
17. [Contributing](#contributing)
18. [License and Acknowledgments](#license-and-acknowledgments)

---

## What It Can Do

### Core Capabilities

| Capability | Description |
|------|------|
| **Three-stage translation** | Full-document analysis → batched translation → per-batch QC. Not a simple "throw it at the LLM" pipeline |
| **Full-document context analysis** | Before translating, the LLM reads the entire subtitle file and produces topic segments, a glossary, speaker profiles, and a style guide |
| **Mandatory terminology corpus** | You call the shots — once 「スパチャ→SC」 is defined, the LLM can never use any other rendering; import from analysis results in one click |
| **Automatic source-language detection** | Mixed Japanese/English subtitles are no problem; each file's language is detected automatically from its content |
| **Translationese removal** | 10+ built-in good/bad example pairs, 6 categories of translation rules, and forced punctuation-free output |
| **Incremental preview** | Watch results appear in real time as translation runs; with multiple files in parallel, switch between each file's results with a dropdown |
| **QC + automatic re-translation** | Japanese residue, missing placeholders, source leakage — each is detected and automatically retried |
| **ASS tag protection** | Formatting tags like `{\i1}{\b1}{\fnXXX}` are stashed and restored automatically during translation; drawing/sound-effect entries are skipped entirely |
| **Batch parallelism** | Translate multiple files simultaneously, with a configurable parallel count |
| **Per-line re-translation + edit persistence** | Right-click any line to re-translate it; manual edits can be saved back to the `_zh` file with one click |
| **Deep glossary optimization** | One click makes the LLM audit the glossary entry by entry — does it sound natural? does the community actually use this phrase? what's missing? |

### Supported Formats

- **Input**: SRT (SubRip), ASS (Advanced SubStation Alpha), SSA, VTT (WebVTT)
- **Output**: the same format, with a `_zh` suffix on the filename (e.g. `episode.srt` → `episode_zh.srt`)

### Translation Directions

- Japanese → Simplified Chinese (the primary scenario)
- English → Simplified Chinese
- Japanese / English → Traditional Chinese

---

## Quick Start

### System Requirements

- **Python**: 3.10 or higher
- **OS**: Windows 10/11 (the primary test platform); macOS/Linux should work in theory but are untested
- **Network**: access to the API endpoint of the LLM you configure

### Installation

```bash
git clone https://github.com/your-username/subtitle-translator.git
cd subtitle-translator
pip install -r requirements.txt
```

Contents of `requirements.txt`:

```
pysubs2>=1.8.1        # subtitle parsing (SRT/ASS/VTT)
openai>=2.0.0         # OpenAI / DeepSeek / custom APIs
anthropic>=0.40.0     # Claude API
tiktoken>=0.7.0       # token estimation
cryptography>=42.0.0  # API key encryption (fallback on non-Windows; Windows uses system DPAPI)
darkdetect>=0.8.0     # system light/dark theme detection
PyQt5>=5.15.11        # GUI framework
```

### Launch

```bash
python main.py
```

---

## Illustrated Tutorial: Your First Translation

### Step 1: Configure the API

Open the app → menu bar **设置 (Settings) → 设置 (Settings)** → the **API 密钥 (API Key)** tab.

1. Choose your service in the "LLM 提供商 (LLM Provider)" dropdown (e.g. DeepSeek)
2. Paste your key into the "API Key" input field
3. "API 端点 (API Endpoint)" auto-fills with that provider's default address
4. Click **「测试连接 (Test Connection)」** at the bottom right to verify the configuration

> **Tip**: testing the connection does **not** save your key permanently — it only verifies the connection. Clicking "Cancel" restores the old configuration.

### Step 2: Model and Language Settings

Switch to the **模型 (Model)** tab:

1. Pick a model in the "模型 (Model)" dropdown (e.g. `deepseek-v4-flash`)
2. Pick `1M` in the "上下文预设 (Context Preset)" dropdown (or adjust "自定义数值 (Custom Value)" directly)
3. Keep 温度 (Temperature) at the default `0.3` (lower = more stable, higher = more creative)

Switch to the **语言 (Language)** tab:

1. Set the source language to 「日语 (Japanese)」
2. Set the target language to 「简体中文 (Simplified Chinese)」

Switch to the **翻译选项 (Translation Options)** tab:

1. Set 分析模式 (Analysis Mode) to 「全文发送 (Send Full Text)」 (DeepSeek's 1M context is more than enough)
2. Leave everything else at default

Click **确定 (OK)** to save.

### Step 3: Load Subtitles

1. **Drag and drop** SRT/ASS files straight onto the file list area on the left
2. Or click the 「添加文件 (Add Files)」 button to select them manually
3. Each file has a checkbox — you can translate only a subset of the files

The file status column shows current progress: 「待翻译 (Pending)」 → 「分析中 (Analyzing)」 → 「待确认分析 (Analysis Awaiting Confirmation)」 → 「翻译中 (Translating)」 → 「✓ 完成 (Done)」

### Step 4: Start Translation

Check the files you want to translate → click **「▶ 开始翻译 (Start Translation)」** in the top toolbar.

Here's what happens:

1. The status bar at the bottom shows progress
2. The LLM performs the full-document analysis (usually 5–30 seconds)
3. When analysis finishes, translation **auto-pauses** and an analysis panel appears on the left

### Step 5: Review the Analysis

The 「全篇分析 (Full-Document Analysis)」 panel on the left shows what the LLM found:

- **Topic segments** (with time ranges and keywords) — double-click to edit
- **Glossary** (source term → translation) — double-click for an edit dialog, or add entries with the `+` button
- **Translation style guide** — edit directly in the text box

Here you can:
- Change the translation of any term
- Delete topic segments that are wrong
- Adjust the style guide
- Click 「深度优化术语 (Deep Optimize Terms)」 to have the LLM re-examine the glossary

Once you're satisfied, click **「确认分析，继续翻译 → (Confirm Analysis, Continue Translation →)」**.

To skip analysis entirely, click **「跳过分析，直接翻译 (Skip Analysis, Translate Directly)」**.

> **Translating multiple files at once**: when files run in parallel, the analysis panel walks through each file awaiting confirmation one by one. Clicking **「确认全部 (Confirm All)」** first confirms the current file with your edits, then auto-confirms every remaining file awaiting confirmation — one action releases the whole batch to translate, no need to click through each one. Every file still keeps its own glossary and style guide.

### Step 6: View Results

The 「对照预览 (Side-by-Side Preview)」 panel on the right shows translation progress in real time:

- `⏳` = waiting to translate (gray)
- `✓` = translation passed
- `⚠` = needs manual review (highlighted yellow)
- `✗` = translation failed

You can **edit translations directly in the preview table** — double-click a cell to modify it. The right-click menu lets you copy the source/translation, or mark a `⚠` entry as reviewed.

### Step 7: Export

1. When translation finishes, results are automatically saved as `原文件名_zh.原扩展名` (original_filename_zh.original_extension) in the same directory as the source file
2. To export to a different folder → check the files → click **「导出所选 (Export Selected)」** → choose the destination directory

---

## The Translation Engine: Three-Stage Pipeline

This is not a simple "hand it to the LLM" translation. The whole flow runs in three interlocking stages:

### Stage 1: Full-Document Analysis (`core/analyzer.py`)

```
Input: the full subtitle file (or three consecutive samples)
Output: AnalysisResult { topic_segments, speakers, glossary, style_guide }
```

The LLM is given a prompt with **built-in thinking steps** — rather than being told to output JSON outright, it is first guided through:

1. **Scan the structure**: how many stages does this stream have? where are the time breaks?
2. **Identify the speakers**: male or female? what tone of voice? verbal tics? how do they read out comments?
3. **Pin down terminology**: using a 90+ word reference checklist, find the words in this subtitle file that need a consistent translation
4. **Write a style guide**: degree of colloquialism, politeness strategy, how to handle emotion

The terminology reference list in the prompt comes from real fan-sub community vocabulary (Moegirl Wiki, Bilibili columns) and spans 6 broad categories. After seeing these references, the LLM proactively recognizes words with similar patterns.

**Translation pauses after analysis** so you can review and edit. This step is the cornerstone of translation quality — everything downstream depends on this analysis.

### Stage 2: Batched Translation (`core/chunker.py` + `core/translator.py`)

```
Input: list of subtitle entries + AnalysisResult
Output: list of translated entries
```

**Chunking strategy**:

- Each batch targets 2000 tokens (configurable)
- **5-entry overlap** between batches: the last 5 entries of each batch are carried into the next batch as context (they don't count toward that batch's translated output)
- Breaks are preferred at time gaps **>5 seconds** (scene changes)
- Overlap entries don't consume the main entries' token budget

**Each batch's translation prompt includes**:

```
## Basic rules (6 items, including mandatory punctuation-free output)
## JA→ZH translation notes
  ├── subject omission
  ├── sentence restructuring (cut と思います entirely, don't force じゃない)
  ├── particle handling (ね/よ/わ are not translated literally)
  ├── politeness strategy (です・ます → natural Chinese)
  ├── long-sentence splitting (≤25 characters per line)
  ├── live-stream gaming specials (screams, laughter, reading comments)
  ├── annotation conventions
  └── red lines (forbidden items)
## Current scene (topic context)
## Mandatory terms (corpus definitions)
## AI-suggested terms (generated by LLM analysis)
```

### Stage 3: Per-Batch QC (`core/qc_checker.py`)

Each batch is checked automatically after translation:

| Check | Criterion | On failure |
|--------|------|----------|
| **Empty translation** | translation is 0 characters long | triggers re-translation |
| **Source left untranslated** | translation == source | triggers re-translation |
| **Missing placeholders** | `<TAG_N>` count mismatch | triggers re-translation |
| **Extra placeholders** | translation contains TAGs that aren't in the source | triggers re-translation |
| **Japanese residue** | kana residue ratio > 30% | triggers re-translation |
| **Translation too short** | source >3 characters but translation ≤1 character | triggers re-translation |

- If severe issues (empty / untranslated / missing placeholders) exceed 20% of a batch → **re-translate the whole batch**
- Retry at most 2 times (configurable)
- Still failing after 2 tries → mark `⚠ needs review` and continue to the next batch (doesn't block the overall flow)

Re-translation comes with the **issue list from the previous round** attached, so the LLM knows exactly what went wrong.

---

## Core Concept: The Terminology Database (Corpus)

### Why You Need a Corpus

The glossary generated by full-document analysis has two problems:

1. **It doesn't know community conventions**: vtuber fans call 「スパチャ」 "SC", not 醒目留言; 「同接」 stays 同接, not 同时在线人数 (concurrent viewers)
2. **It's unstable across runs**: the same term can be translated as A this time and B next time

The corpus solves this — **the translation rules you define are mandatory for the LLM**.

### Priority

```
Corpus (mandatory) > user-edited analysis result > LLM full-document analysis result
```

### How to Use It

In the tab bar on the right → **「术语库 (Corpus)」**:

1. Click `+ 添加 (Add)` and enter a source term and its translation
2. Double-click a cell to edit directly
3. Select a row → `- 删除 (Delete)`
4. Use the search box at the top to filter quickly

### How It Works

1. **Analysis stage**: corpus terms are injected into the analysis prompt as "known terms" — the LLM knows these words already have fixed translations and won't emit conflicting versions in its own glossary
2. **Translation stage**: the glossary is split into two layers — "mandatory terms" (those that exist in the corpus, marked "must be used exactly, do not modify") and "AI-suggested" (generated by the LLM)
3. **Conflict resolution**: if the LLM-generated glossary contains a term already present in the corpus, the corpus version wins outright

### Difference from `glossary_vtuber.json`

| File | Purpose | Editable | Priority |
|------|------|--------|--------|
| `corpus_vtuber.json` | **Terminology database** — your mandatory rules | ✓ (UI or editor) | Highest |
| `glossary_vtuber.json` | **Preset reference** — 112 fan-sub terms | ✓ (editor) | Reference |

> Simply put: in `corpus` you're the boss; `glossary` is just a reference list for the LLM to look at.

> **How the preset reference is injected**: the full list is injected into the analysis prompt during analysis; during translation, only the preset terms that **actually appear in the current batch** are injected — controlling token cost without sacrificing terminology consistency.

### Recommended Initial Configuration

```
スパチャ → SC
同接 → 同接
コラボ → 联动
配信 → 直播
初配信 → 出道直播
切り抜き → 切片
初見 → 新来的
草 → 草
www → www
```

Read each line as "term → its fixed translation" (these target Simplified Chinese subtitles): スパチャ → SC (superchat), 同接 → 同接 (concurrent viewers — kept as-is), コラボ → 联动 (collaboration), 配信 → 直播 (stream), 初配信 → 出道直播 (debut stream), 切り抜き → 切片 (clip), 初見 → 新来的 (first-time viewer), 草 → 草 (the Japanese net-slang for "lol" — kept as-is), www → www (laughing — kept as-is).

---

## Prompt Engineering: Why It Translates So Well

Roughly 80% of translation quality comes from the prompts. Here is the design rationale behind each component.

### 1. Full-Document Analysis Prompt (JA→ZH)

**Location**: `core/analyzer.py` → `ANALYSIS_PROMPT_JA`

**Design rationale**:

- ❌ No "please analyze the following and output JSON" → the LLM wouldn't know what to do
- ✅ A **four-step thinking flow**: scan structure → identify speakers → pin down terms → write the style guide
- ✅ Each step has **concrete guiding questions** ("Is the streamer male or female? Judge from their self-reference")
- ✅ Step 3 includes a **90+ word categorized reference list** (streaming / fans / gaming / chat / ACG) — not for the LLM to copy, but to activate its sensitivity to these word patterns
- ✅ **Negative examples**: 「枠→画框 (frame — wrong)」「凸待ち→等待凸起 (waiting for the bulge — wrong)」
- ✅ The JSON example is **non-empty** — every field is filled with realistic data, so the LLM imitates that format and level of detail

### 2. Translation Prompt (JA→ZH)

**Location**: `services/prompt_builder.py` → `_ja_to_zh_guidelines()`

**Design rationale**:

- ❌ No "avoid translationese"
- ✅ **10 good/bad example pairs**, each a "❌ translationese → ✅ natural Chinese"
- ✅ Rules are grouped: subject / sentence pattern / particles / politeness / long sentences / live-stream gaming / annotations / red lines
- ✅ Every rule carries a **concrete example**: 「〜と思います → cut it out entirely, not 『我觉得 (I think)』」
- ✅ The core mindset goes first: "let the audience not feel the translation exists" and "channel a friend you're talking to in real life"
- ✅ **Mandatory punctuation-free output**: rule #6 + red line #1 + every example is punctuation-free

### 3. Glossary Deep-Optimization Prompt

**Location**: `core/analyzer.py` → `GLOSSARY_OPTIMIZE_PROMPT`

**Design rationale**:

- Four review dimensions: naturalness / domain accuracy / missing-term check / redundancy cleanup
- Each dimension has **correct and incorrect examples**
- Raw subtitle fragments are attached as context for reference

### 4. Analysis Prompt × Corpus Integration

The end of the analysis prompt is injected with the **known-terms table** (from the corpus), telling the LLM: "these terms already have fixed translations — use them as-is in your glossary, don't invent your own". This solves the problem of the LLM re-emitting translations that already exist.

---

## Supported LLMs and Configuration

### Provider Comparison

| | OpenAI | Anthropic Claude | DeepSeek | Custom |
|---|---|---|---|---|
| **Default model** | gpt-5-mini | claude-sonnet-5 | deepseek-v4-flash | enter your own |
| **Context window** | 400K (gpt-5) / 1M (gpt-4.1) | 200K (Claude 5) / 1M (Fable 5) | 1M | configurable |
| **Chinese quality** | ★★★★ | ★★★★ | ★★★★★ | — |
| **Speed** | ★★★ | ★★★ | ★★★★ | — |
| **Cost (per 1M tokens)** | input $2.5 / output $10 | input $3 / output $15 | input ¥1 / output ¥2 | — |
| **Prompt Caching** | ✓ automatic | ✓ requires a marker | — | — |
| **Recommended analysis mode** | smart sampling | smart sampling | send full text | case-dependent |

### Configuration Examples per Provider

**DeepSeek** (recommended, best value):

```
提供商: DeepSeek
模型: deepseek-v4-flash
API 端点: https://api.deepseek.com
上下文窗口: 1M (1000000)
温度: 0.3
分析模式: 全文发送
分析阶段预留: 64000
```

> Note: the DeepSeek endpoint does **not** need a `/v1` suffix. `deepseek-chat` and `deepseek-reasoner` are being deprecated — use `deepseek-v4-flash` and `deepseek-v4-pro` instead.

**OpenAI**:

```
提供商: OpenAI
模型: gpt-5-mini
API 端点: https://api.openai.com/v1
上下文窗口: 400000
温度: 0.3
分析模式: 智能取样
```

**Claude**:

```
提供商: Anthropic (Claude)
模型: claude-sonnet-5
API 端点: （留空使用官方）
上下文窗口: 200000
温度: 0.3
分析模式: 智能取样
```

**Ollama / local models**:

```
提供商: 自定义 OpenAI 兼容 API
模型: qwen2.5:7b（或你的模型名）
API 端点: http://localhost:11434/v1
上下文窗口: 4096
温度: 0.3
分析模式: 智能取样
```

### Available Models per Provider

**OpenAI**: gpt-5 / gpt-5-mini / gpt-4o / gpt-4o-mini / gpt-4.1 / gpt-4.1-mini / gpt-4.1-nano / gpt-4-turbo

**Anthropic**: claude-fable-5 / claude-opus-5 / claude-sonnet-5 / claude-haiku-4-5-20251001 / claude-sonnet-4-20250514 / claude-opus-4-20250514 / claude-haiku-4-20250514

**DeepSeek**: deepseek-v4-flash / deepseek-v4-pro / deepseek-chat (old-name compatibility) / deepseek-reasoner (old-name compatibility)

> The model dropdown accepts any model name typed manually (not limited to the list).

**Custom**: enter any model name yourself

---

## Settings Reference

### API 密钥 (API Key) — Tab 1

| Setting | Description |
|------|------|
| LLM 提供商 (LLM Provider) | OpenAI / Anthropic / DeepSeek / Custom |
| API Key | Password field; the plaintext is never visible (saved keys display as `••••••••`) |
| API 端点 (API Endpoint) | Fills in the provider's default endpoint automatically; editable |
| 测试连接 (Test Connection) | Temporarily verifies the connection with the current input (**not saved permanently**; Cancel restores the old values) |

> **Security note**: on Windows, the API key is stored with **DPAPI** (encrypted for the current user) in `%APPDATA%/SubtitleTranslator/keys.enc`, bound to the current Windows user account — it can't be copied to another machine/user, and no plaintext ever sits on disk. Older versions (Fernet with a plaintext key file) are auto-migrated on first launch. Each provider's key is stored independently.

### 模型 (Model) — Tab 2

| Setting | Default | Description |
|------|--------|------|
| 模型 (Model) | depends on the provider | dropdown with common models + manual input |
| 上下文预设 (Context Preset) | — | quick options: 4K/8K/16K/32K/64K/128K/200K/1M/custom |
| 自定义数值 (Custom Value) | 64000 | the exact context window ceiling (tokens) |
| 温度 (Temperature) | 0.3 | 0.0–2.0; lower = more stable |
| 最大重试 (Max Retries) | 3 | retry count after failed API calls |

### 语言 (Language) — Tab 3

| Setting | Options |
|------|------|
| 源语言 (Source Language) | 日语 (Japanese) / 英语 (English) / 自动检测 (Auto-detect) |
| 目标语言 (Target Language) | 简体中文 (Simplified Chinese) / 繁体中文 (Traditional Chinese) |

### 翻译选项 (Translation Options) — Tab 4

| Setting | Default | Description |
|------|--------|------|
| 每批目标大小 (Target Batch Size) | 2000 tokens | presets: 500/1000/2000/3000/5000 |
| Overlap 条数 (Overlap Count) | 5 | presets: 0/3/5/10 |
| 分析阶段预留 (Analysis Reservation) | 16000 tokens | presets: 4K–1M |
| 分析模式 (Analysis Mode) | 智能取样 (smart sampling) | 全文发送 (send full text) / 智能取样 (smart sampling) |
| 并行文件数 (Parallel Files) | 2 | 1–8; translate multiple files simultaneously |
| 质检最大重试 (Max QC Retries) | 2 | how many times a single batch may be retried |

> Parallel translation runs on multiple QThread workers: several files analyze/translate at once, but they **share a single rate limiter** (so parallelism never blows past your API quota). Each file's analysis still waits for your confirmation in the left panel before continuing.

### 外观 (Appearance) — Tab 5

| Setting | Description |
|------|------|
| 背景颜色 (Background Color) | pick any color with the color picker, or type a hex value |
| 窗口透明度 (Window Opacity) | 30%–100% slider |
| 背景图片 (Background Image) | PNG/JPG, centered |
| 预览效果 (Preview Effect) | live preview of the current settings |
| 恢复默认 (Restore Defaults) | clears all custom appearance |

---

## Project Architecture

### Layered Structure

```
┌─────────────────────────────────────────┐
│  main.py  — entry point                 │
├─────────────────────────────────────────┤
│  gui/  — UI layer                       │
│  ├── main_window.py     Main window     │
│  ├── file_panel.py      File list       │
│  ├── preview_panel.py   Preview panel   │
│  ├── analysis_panel.py  Analysis editor │
│  ├── corpus_panel.py    Corpus panel    │
│  ├── settings_dialog.py Settings dialog │
│  ├── theme_manager.py   Theme manager   │
│  ├── progress_widget.py Progress bar    │
│  └── editor_dialog.py   Entry editor    │
├─────────────────────────────────────────┤
│  core/  — business logic layer          │
│  ├── translator.py      Translation     │
│  │                      orchestrator    │
│  ├── analyzer.py        Full-doc        │
│  │                      analyzer        │
│  ├── chunker.py         Smart chunker   │
│  ├── qc_checker.py      QC checker      │
│  ├── merger.py          Merger          │
│  ├── corpus_manager.py  Corpus manager  │
│  ├── tag_handler.py     ASS tag handler │
│  └── subtitle_io.py     Subtitle I/O    │
├─────────────────────────────────────────┤
│  services/  — external service layer    │
│  ├── base_llm.py        Abstract        │
│  │                      interface       │
│  ├── openai_service.py  OpenAI-compat   │
│  │                      implementation  │
│  ├── claude_service.py  Claude impl     │
│  ├── prompt_builder.py  Prompt builder  │
│  └── rate_limiter.py    Rate limiter    │
├─────────────────────────────────────────┤
│  config/  — config layer                │
│  │   └── settings.py    QSettings       │
│  │                      wrapper         │
│  utils/   — utility layer               │
│  │   ├── constants.py   Constants       │
│  │   ├── logger.py      Logging         │
│  │   └── token_counter.py Token count   │
│  resources/  — resource files           │
│      ├── corpus_vtuber.json  Corpus     │
│      ├── glossary_vtuber.json Reference │
│      └── styles/           QSS themes   │
└─────────────────────────────────────────┘
```

### Key Design Decisions

**Why QThread instead of asyncio?**
PyQt5's event loop requires all GUI operations on the main thread. Translation is I/O-bound (API calls), so it runs in QThread workers. Workers report progress to the main thread via Qt signals (queued across threads automatically). This keeps the GUI responsive while translation runs in the background.

**Why doesn't the translation prompt use JSON?**
The numbered-line format `N. 译文` uses fewer tokens than JSON, has better LLM compliance, and is more robust to parse. JSON is prone to syntax errors (one missing comma ruins the whole thing); the numbered-line format is handled by a single regex `^(\d+)\.\s+(.*)$`.

**Why are ASS tags replaced with placeholders instead of translated directly?**
LLMs don't recognize ASS tags like `{\i1}` and treat them as plain text. Replacing them with `<TAG_0><TAG_1>` placeholders and restoring them after translation guarantees the formatting tags survive intact. Drawing blocks `{\p1}...{\p0}` are detected and skipped entirely.

**Why does the corpus take priority over LLM analysis?**
Each analysis may produce different term translations (「スパチャ→SC」 one time, 「スパチャ→醒目留言」 the next). The corpus is the user's single source of truth — mandatory terms cannot be tampered with, regardless of the analysis result.

---

## Data Flow in Detail

### The Complete Translation Data Flow

```
User drags in a file
  │
  ▼
FilePanel ──filepaths──▶ MainWindow._on_translate()
  │
  ▼
TranslatorFacade.start_translation()
  │
  ├─► prepare_translation()  creates N QThread+Worker pairs (N = parallel file count, files split evenly)
  ├─► connects Qt signals (the Facade aggregates all workers' signals and forwards them to the UI)
  └─► start()  starts all threads
        │
        ▼
      TranslatorWorker.run()   (each worker processes its own assigned files)
        │
        ▼  Phase 1
      SubtitleFile.load() ──entries──▶ SubtitleAnalyzer.build_analysis_prompt()
        │                                    │
        │                              LLM.analyze(prompt)
        │                                    │
        │                              parse_analysis_response()
        │                                    │
        │                         ┌─ AnalysisResult ─┐
        │                         │  · topic_segments │
        │                         │  · speakers       │
        │                         │  · glossary       │
        │                         │  · style_guide    │
        │                         └──────────────────┘
        │                                    │
        │                    ┌─── signal: file_analysis_done ───▶ GUI displays it
        │                    │
        │                    ├─── signal: waiting_for_approval ──▶ GUI waits
        │                    │                                      │
        │                    │       user edits analysis + clicks confirm ◀────┘
        │                    │         │
        │                    │  approve_analysis(filepath, edited)
        │                    │    Facade routes to the matching worker by filepath
        │                    │         │
        │                    ▼         ▼
        │  Phase 2         CorpusManager + AnalysisResult
        │                    │
        │              SubtitleChunker.chunk(entries, analysis)
        │                    │
        │              ┌── TranslationBatch[0]
        │              │    └── entries + topic_context + glossary
        │              ├── TranslationBatch[1]
        │              │    └── entries + topic_context + glossary (+ overlap)
        │              ├── ...
        │              └── TranslationBatch[N]
        │
        │  per-batch loop:
        │    prompt_builder.build_translation_prompt(batch)
        │      │
        │      ├── basic rules + JA→ZH translation notes
        │      ├── current scene (topic context)
        │      ├── mandatory terms (corpus)   ◀── CorpusManager.split_terms()
        │      └── AI-suggested terms (LLM-generated)
        │
        │    LLM.translate_batch(batch) ──▶ translated_texts
        │      │
        │      ▼  Phase 3
        │    QCChecker.check(entries, translated_texts)
        │      │
        │      ├── PASS ──▶ next batch
        │      └── FAIL ──▶ retry (max 2) ──▶ still failing ──▶ mark ⚠ needs review
        │
        │    signal: batch_completed ──▶ GUI incrementally updates preview
        │
        ▼
      Merger.merge(original, results) ──▶ list of translated entries
        │
      SubtitleFile.save(output_path, merged)
        │
      signal: file_completed ──▶ GUI shows completion
```

### The Glossary Data Flow

```
corpus_vtuber.json ──load──▶ CorpusManager._terms
                                  │
                   ┌──────────────┤
                   │              │
                   ▼              ▼
           analyzer.py       prompt_builder.py
           (informs the      (splits mandatory /
            LLM of known     suggested terms in
            terms in the     the translation
            analysis prompt) prompt)
                   │              │
                   ▼              ▼
              LLM analysis    LLM batch
              glossary        translation
```

---

## Developer Guide

### Adding a New LLM Provider

Taking 「智谱 GLM (Zhipu GLM)」 as an example:

**1. Register the provider**

`utils/constants.py`:

```python
LLM_PROVIDERS = {
    ...
    "zhipu": "智谱 GLM",
}

PROVIDER_MODELS = {
    ...
    "zhipu": ["glm-4-plus", "glm-4-flash"],
}

DEFAULT_MODELS = {
    ...
    "zhipu": "glm-4-flash",
}

DEFAULT_API_BASE_URLS = {
    ...
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
}

MODEL_CONTEXT_WINDOWS = {
    ...
    "glm-4-plus": 128000,
    "glm-4-flash": 128000,
}
```

**2. OpenAI-compatible providers just work**

Zhipu's API is OpenAI-compatible → no new service needed; `OpenAIService` supports it natively.

Users can simply pick 「自定义 OpenAI 兼容 API (Custom OpenAI-compatible API)」 in settings and fill in the endpoint and model name. To make 「智谱 GLM」 appear as its own provider in the dropdown, just configure the constants in step 1.

**3. Non-OpenAI-compatible providers**

Implement the `BaseLLMService` interface in `services/base_llm.py`:

```python
class ZhipuService(BaseLLMService):
    def translate_batch(self, batch, source_lang, target_lang) -> list[str]: ...
    def analyze(self, prompt: str) -> str: ...
    def validate_api_key(self) -> bool: ...

    @property
    def provider_name(self) -> str: return "zhipu"
    @property
    def default_model(self) -> str: return "glm-4-flash"
```

Then add a branch in `_get_llm_service()` in `core/translator.py`.

### Key Class Interfaces

<details>
<summary><b>BaseLLMService</b> (abstract interface)</summary>

```python
class BaseLLMService(ABC):
    @abstractmethod
    def translate_batch(self, batch: TranslationBatch,
                        source_lang: str, target_lang: str) -> list[str]: ...
    @abstractmethod
    def analyze(self, prompt: str) -> str: ...
    @abstractmethod
    def validate_api_key(self) -> bool: ...

    @property
    @abstractmethod
    def provider_name(self) -> str: ...
    @property
    @abstractmethod
    def default_model(self) -> str: ...
```
</details>

<details>
<summary><b>TranslationBatch</b> (data class)</summary>

```python
@dataclass
class TranslationBatch:
    batch_id: int              # batch number
    entries: list[SubtitleEntry]  # all entries (including overlap)
    primary_start_idx: int     # start of new entries (excluding overlap)
    primary_end_idx: int       # end of new entries
    glossary: dict[str, str]   # cumulative glossary
    topic_context: str         # current topic
    estimated_tokens: int      # token estimate
```
</details>

<details>
<summary><b>AnalysisResult</b> (data class)</summary>

```python
@dataclass
class AnalysisResult:
    topic_segments: list[dict]  # [{start_idx, end_idx, topic, key_terms}]
    speakers: list[dict]        # [{speaker, tone, speech_patterns}]
    glossary: dict[str, str]    # {Japanese: Chinese}
    style_guide: str            # translation style recommendations
```
</details>

<details>
<summary><b>CorpusManager</b> (corpus manager)</summary>

```python
class CorpusManager:
    def get_all(self) -> dict[str, str]: ...
    def get(self, term: str) -> str | None: ...
    def add(self, term: str, translation: str) -> None: ...
    def remove(self, term: str) -> None: ...
    def update_all(self, terms: dict) -> None: ...
    def resolve_conflicts(self, llm_glossary: dict) -> dict: ...
    def split_terms(self, llm_glossary: dict) -> tuple[dict, dict]: ...
    @staticmethod
    def get_preset_terms() -> dict[str, str]: ...  # glossary_vtuber.json reference list
```
</details>

### Thread Model

```
Main Thread                     Worker Threads (N QThreads, N = parallel file count)
     │                                │           │
     ├─ TranslatorFacade()            │           │
     ├─ prepare_translation() ──────► │  worker1  │  worker2 ...
     │   files split evenly           │           │
     ├─ connect signals ◄─────────── │  ────────► │  each worker's signals are
     │   (Facade aggregates and      │  ════════► │  forwarded to the UI
     │    forwards)                   │           │  by the Facade
     ├─ start() ────────────────────► │  run()    │  run()
     │                                ├─ _translate_one_file()
     │  ◄── signal: file_started ────┤
     │  ◄── signal: analysis_done ───┤
     │  ◄── signal: waiting ─────────┤  [BLOCKED on _continue_event]
     │                                │
     │  approve_analysis(filepath,..)─►  Facade routes to the matching worker by filepath
     │                                ├─ chunk + translate
     │  ◄── signal: batch_done ──────┤
     │  ◄── signal: file_done ───────┤
     │  ◄── signal: all_completed ───┤  the Facade reports only once after all workers finish
     │                                └─ run() returns
     └─ _on_all_completed()
```

> Each file's analysis must be confirmed by you in the left panel (with multiple files in parallel, the panel walks through them one by one).

### Debugging

**View the logs**:

```
%APPDATA%/SubtitleTranslator/logs/translator.log
```

Rotates automatically, keeping 5 files of 2 MB each.

**Python debug mode**:

```bash
python -c "
import logging
logging.basicConfig(level=logging.DEBUG)
import sys; sys.path.insert(0, '.')
from core.analyzer import SubtitleAnalyzer
# ... test code ...
"
```

**View the generated prompts**:

```python
from services.prompt_builder import build_translation_prompt
from core.translation_batch import TranslationBatch, SubtitleEntry

batch = TranslationBatch(...)
prompt = build_translation_prompt(batch, 'ja', 'zh-CN')
print(prompt)
```

---

## Testing

The project has two test layers: **unit tests** (pytest, `tests/`) and **end-to-end verification scripts** (`tests/e2e/`, using a fake LLM — no network, no tokens spent).

### Unit Tests

```bash
pip install -e ".[dev]"   # installs pytest
pytest                    # 149 tests, ~10s
```

### End-to-End Verification

```bash
python tests/e2e/run_e2e.py          # one-click run of all e2e scripts
python tests/e2e/test_parallel.py    # parallel translation end-to-end (multi-file parallel + per-file confirm + result aggregation)
python tests/e2e/test_parallel_gui.py # triggers translation through the real main window (signal wiring + panel-confirm routing)
python tests/e2e/test_preview_parallel.py # parallel preview state isolation + per-line re-translation + saving back to file
python tests/e2e/test_real_files.py  # robustness with real-style files (BOM/CRLF/HTML tags/multiline/ASS drawing/style preservation)
python tests/e2e/test_parser.py      # LLM output parsing robustness + JSON trailing commas + batch confirmation
```

> The real-file tests also verify a detail: pysubs2 silently drops `{\b}` bold markers when saving SRT, and the tool ships a built-in fix (converting them back to `<b>/</b>` before saving). `<i>/<u>` are preserved natively by pysubs2.

> Note: the e2e scripts read and write your machine's real settings (QSettings) and the `resources/corpus_vtuber.json` corpus, and may leave test traces behind. It's recommended to run them in a clean test environment before release.

### Manual Functional Test Checklist

- [ ] The app starts correctly (`python main.py`)
- [ ] All 5 tabs in the settings dialog switch properly
- [ ] Model, endpoint and key switch correctly when changing providers
- [ ] The connection test works (requires a valid API Key)
- [ ] Dragging SRT/ASS files adds them to the list; 「移除选中 (Remove Selected)」 deletes a row
- [ ] Checkboxes and file list status are correct
- [ ] Full-document analysis completes and displays results
- [ ] Analysis panel editing (topics, terms, style guide) works
- [ ] The deep glossary optimization button triggers and returns results
- [ ] Batched translation + incremental preview works; the dropdown switches between each file's results in parallel mode
- [ ] The QC retry mechanism works
- [ ] Results are correctly saved as `_zh` files
- [ ] Right-click 「重新翻译此条 (Re-translate This Line)」 takes effect and writes back; 「保存修改 (Save Changes)」 works
- [ ] 「从分析导入 (Import from Analysis)」 merges analysis terms into the corpus
- [ ] With source language set to 「自动检测 (Auto-detect)」, both Japanese and English subtitles translate correctly
- [ ] Export works
- [ ] Theme switching (light/dark) works
- [ ] Custom background (color, opacity, image) works
- [ ] Corpus panel add/edit/delete works
- [ ] Cancel translation works

### Test Subtitles

A test file ships with the project: `vtuber_test.srt` — 34 Japanese subtitle entries simulating a full vtuber live-stream archive.

---

## Performance and Cost

### Token Consumption Estimate

Using **100 Japanese subtitle entries** (roughly a 2–3 minute stream segment) as an example:

| Stage | Token consumption | Notes |
|------|-----------|------|
| Full-document analysis | 5K–30K | depends on subtitle length and analysis mode |
| Each batch translation | 2K–5K | depends on the configured batch size |
| Total batches | ~4–8 | 100 entries ÷ 20 entries/batch ≈ 5 batches |
| **Total** | **~30K–50K tokens** | — |

### Cost Comparison Across Models (per 100 subtitle entries)

| Model | Input cost | Output cost | **Subtotal** |
|------|----------|----------|----------|
| DeepSeek V4 Flash | ¥0.03 | ¥0.06 | **≈ ¥0.09** |
| GPT-4o | $0.08 | $0.30 | **≈ $0.38** |
| GPT-4o-mini | $0.005 | $0.03 | **≈ $0.035** |
| Claude Sonnet 4 | $0.09 | $0.45 | **≈ $0.54** |

> DeepSeek V4 Flash is the most economical choice — translating 1000 subtitle entries costs under ¥1.

### Speed Estimates

| Model | Analysis time | Translation per batch | 100 entries total |
|------|----------|-------------|-------------|
| DeepSeek V4 Flash | 5–15s | 3–8s | **30–90s** |
| GPT-4o | 10–25s | 5–15s | **60–180s** |
| GPT-4o-mini | 8–15s | 3–8s | **40–100s** |
| Claude Sonnet 4 | 10–20s | 8–20s | **80–200s** |

---

## Troubleshooting

| Symptom | Likely cause | Solution |
|------|----------|----------|
| File status stays on 「待翻译 (Pending)」 after launch | signals not wired up correctly | restart the app |
| Clicking start translation does nothing | 1) no API Key configured 2) old thread not cleaned up | 1) check the API Key in settings 2) restart the app |
| Doesn't continue after analysis | **normal behavior** — translation is waiting for you to click "confirm and continue" in the left panel | review the analysis → click the button to continue |
| Translation stalls halfway | API call timeout or rate limiting | wait (up to 90s × 3 retries), or click 「取消 (Cancel)」 and retry |
| Translationese is heavy | corpus not configured / the LLM model isn't capable enough | 1) configure common corpus terms 2) run deep glossary optimization 3) switch to a stronger model |
| Connection test fails | wrong key / wrong endpoint / network issue | check the key and whether the endpoint is reachable |
| Settings still changed after clicking Cancel in the dialog | the Test Connection button temporarily modifies settings | fixed: settings are auto-restored after a connection test |
| ASS formatting lost after translation | pysubs2 parse issue | the app auto-detects UTF-8/GBK/Shift-JIS encoding; if it still fails, save the file manually as UTF-8 |
| Error loading non-UTF-8 subtitles | rare unrecognized encodings | re-save as UTF-8 with a text editor |
| Preview panel is empty | subtitle file failed to load | check the file path and format |
| Glossary is empty | LLM analysis produced no glossary | the analysis prompt already requires at least 5 terms; switch to a stronger model or reduce subtitle complexity |

### Log Location

```
%APPDATA%\SubtitleTranslator\logs\translator.log
```

When you hit a problem, look for `[WARNING]` and `[ERROR]` entries in the log.

---

## Packaging and Distribution

### Packaging as a Windows .exe

The project ships with `subtitle_translator.spec` (single-file, no console, auto-collects `resources/`):

```bash
pip install pyinstaller
pyinstaller subtitle_translator.spec --noconfirm
```

The artifact lands in `dist/字幕翻译工具.exe` (≈74 MB, verified with PyInstaller 6.18).

### Resource Files to Include

`corpus_vtuber.json`, `glossary_vtuber.json` and `styles/` under `resources/` are auto-collected by the spec's `datas`. The code locates them via `Path(__file__).parent.parent / "resources"`, which resolves to `sys._MEIPASS/resources` in a bundled environment — no extra handling needed.

> If you package via the command line yourself, remember to add `--add-data "resources;resources"`.

---

## FAQ

<details>
<summary><b>Q: Why doesn't translation continue after analysis finishes?</b></summary>

**By design** — translation pauses after analysis so you can review and edit the result. Look at the left panel, and once everything is correct click 「确认分析，继续翻译 → (Confirm Analysis, Continue Translation →)」. If you don't need analysis, click 「跳过分析，直接翻译 (Skip Analysis, Translate Directly)」.
</details>

<details>
<summary><b>Q: Does the DeepSeek endpoint need /v1 or not?</b></summary>

**No.** DeepSeek's current API endpoint is `https://api.deepseek.com`, without `/v1`. If your old scripts use `/v1`, they still work, but the official docs recommend dropping it.
</details>

<details>
<summary><b>Q: The Chinese output still sounds a bit off?</b></summary>

Troubleshoot in this order:
1. Is the corpus configured? — add entries like 「スパチャ→SC」「同接→同接」 to the corpus
2. Did you edit the analysis? — spell out your translation preferences in the style guide
3. Which model are you using? — DeepSeek V4 Flash produces the best Chinese; gpt-4o-mini sometimes gets "lazy"
4. Tried deep glossary optimization? — the 「深度优化术语 (Deep Optimize Terms)」 button in the analysis panel
</details>

<details>
<summary><b>Q: Why is there no punctuation in the translated output?</b></summary>

**Deliberate design.** Video subtitles don't need punctuation — periods, commas and exclamation marks clutter the picture. Sentences are separated with spaces instead, which is cleaner.
</details>

<details>
<summary><b>Q: Can I use local models (Ollama/vLLM)?</b></summary>

Yes. In settings, choose 「自定义 OpenAI 兼容 API (Custom OpenAI-compatible API)」, set the endpoint to `http://localhost:11434/v1` (Ollama) or your vLLM address, and enter the model name you pulled (e.g. `qwen2.5:7b`). Note that local models usually have a smaller context window, so pick 智能取样 (smart sampling) for the analysis mode.
</details>

<details>
<summary><b>Q: What's the relationship between the corpus and glossary_vtuber.json?</b></summary>

- `corpus_vtuber.json` = the terminology database = your **mandatory rules**, highest priority
- `glossary_vtuber.json` = the preset reference = 112 fan-sub terms, injected in full during analysis; during translation only the keywords that actually appear in the current batch

What you edit through the corpus tab in the UI is `corpus_vtuber.json`.
</details>

<details>
<summary><b>Q: Can it translate English subtitles?</b></summary>

Yes. Settings → Language → set the source language to 「英语 (English)」. The analysis and translation prompts both have corresponding English versions.
</details>

<details>
<summary><b>Q: Can I cancel mid-translation?</b></summary>

Yes. Click the 「取消 (Cancel)」 button next to the progress bar at the bottom. Note: if an API call is in flight, you'll have to wait for it to finish or time out (up to 90 seconds) before translation actually stops.
</details>

<details>
<summary><b>Q: Is my API key safe?</b></summary>

On Windows, keys are encrypted with **DPAPI (CryptProtectData)** and stored in `%APPDATA%/SubtitleTranslator/keys.enc`. DPAPI's keys are managed by the OS and bound to the **current Windows user account** — neither another machine nor another user can decrypt them. On non-Windows systems it falls back to Fernet with a machine-ID-derived key. This is **not military-grade security** (if an attacker runs code as your user, DPAPI will happily decrypt), but it's enough to prevent:
- config files from being copied to another machine/user and reused
- a text editor from reading the plaintext key directly
- accidental leaks through chats or screenshots

> Older versions used a plaintext `.keyfile` + Fernet encryption. On first launch, the old format is auto-migrated to DPAPI and the plaintext key file is deleted; if decryption fails, a warning is written to the logs (your keys are no longer silently wiped).
</details>

<details>
<summary><b>Q: Can it translate video files with embedded subtitles?</b></summary>

No. This tool works on **subtitle files** (SRT/ASS/VTT), not video files. You'll need to extract the subtitles from the video with another tool, or download an existing subtitle file.
</details>

---

## Contributing

### Code of Conduct

- Be friendly and respectful
- Accept constructive criticism
- Focus on what's best for the community

### How to Contribute

1. **Fork** this repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Commit your changes: `git commit -m 'Add amazing feature'`
4. Push to the branch: `git push origin feature/amazing-feature`
5. Open a **Pull Request**

### Contribution Directions

| Direction | Description | Difficulty |
|------|------|------|
| 🐛 Bug fixes | Any bug that causes crashes, data loss, or broken behavior | ★★☆ |
| 🌐 New LLM adapters | Add support for a new LLM provider | ★★☆ |
| 📝 Prompt optimization | Improve translation quality — especially translationese removal and domain terms | ★☆☆ |
| 🗂️ Corpus expansion | Add more vtuber terms to `corpus_vtuber.json` or `glossary_vtuber.json` | ★☆☆ |
| 🎨 UI/UX improvements | Visual polish, interaction refinement | ★★★ |
| 📖 Documentation | README, code comments, wiki | ★☆☆ |
| 🧪 Testing | Unit tests, integration tests | ★★★ |
| 📦 Packaging scripts | PyInstaller, CI/CD, automated releases | ★★★ |

### PR Guidelines

- Title format: `[type] short description` (e.g. `[Fix] 修复取消翻译后无法重新开始`)
- Describe clearly: what changed, why, and how it was tested
- One PR does one thing — don't cram unrelated changes into a single PR
- For UI changes, attach screenshots

---

## License and Acknowledgments

### License

MIT License — see the [LICENSE](LICENSE) file.

### Dependencies

| Project | Purpose |
|------|------|
| [pysubs2](https://github.com/tkarabela/pysubs2) | SRT/ASS/VTT subtitle parsing and generation |
| [PyQt5](https://www.riverbankcomputing.com/software/pyqt/) | Desktop GUI framework |
| [OpenAI Python SDK](https://github.com/openai/openai-python) | OpenAI / DeepSeek / custom API calls |
| [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python) | Claude API calls (including Prompt Caching) |
| [tiktoken](https://github.com/openai/tiktoken) | Token-count estimation |
| [cryptography](https://cryptography.io/) | Fernet fallback for API key encryption on non-Windows |
| [darkdetect](https://github.com/albertosottile/darkdetect) | Windows light/dark theme detection |

### Terminology Reference Sources

- [Moegirl Wiki — VTuber terms and memes](https://moegirl.icu/%E8%99%9A%E6%8B%9FUP%E4%B8%BB%E7%94%A8%E8%AF%AD%E4%B8%8E%E6%A2%97)
- [Moegirl Wiki — Finished subtitles (熟肉)](https://mzh.moegirl.org.cn/%E7%86%9F%E8%82%89)
- [Bilibili column — 2.5 years of personal fan-subbing experience](https://www.bilibili.com/read/cv24666246/)
- [Bilibili column — Basic V-sphere terminology](https://www.bilibili.com/read/cv21472693/)
- [Intro to common VTuber terms & recommended JA→ZH channels](https://ayvc0420.github.io/recommendYoutube.html)
- [Nekomoekissaten-Subs fansub group translation standards](https://github.com/Nekomoekissaten-SUB/Nekomoekissaten-Subs/wiki/translation)
- [sakuraa.net — Where unnatural translations come from](https://sakuraa.net/unnatural/)
- [阿改烤肉团 public training (Bilibili video)](https://www.bilibili.com/video/BV14nrCYCEWn/)
- [MOJi辞書 — The fun of subtitle translation](https://www2.mojidict.com/article/6mXN3q6Nns)

---

<p align="center">
  <b>Let the AI roast up finished subs your viewers will love 🥩</b>
</p>
