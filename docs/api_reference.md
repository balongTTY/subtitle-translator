# 模块 API 参考（api_reference）

> 生成基线：2026-09-03 源码。全部条目来自实际代码，不含规划中的接口。
> 线程安全标记约定：
> - **[任意线程]** 可在任意线程调用（无状态或内部有锁）；
> - **[主线程]** 只应在 GUI 主线程调用（QObject 亲和 / QSettings / Qt 对象）；
> - **[单 worker 串行]** 设计给单个工作线程独占使用，多线程共享同一实例未做保护；
> - **[阻塞]** 会阻塞调用线程（网络 / 等待配额 / 磁盘），调用方须自行放到后台线程；
> - **[GIL 依赖]** CPython GIL 构建下安全，free-threaded 构建下需要先加锁。
>
> 异常一栏只列**该函数会抛出或有意上抛**的异常；被内部捕获转换的异常在"行为"中说明。

---

## 1. core 包 — 翻译流水线与字幕域

### 1.1 core.translation_batch — 共享数据类

职责：流水线全层共享的数据结构（零依赖叶子模块，是 core⇄services 事实上的 shared kernel）。

| 类 | 说明 |
|---|---|
| `SubtitleEntry` | 单条字幕条目（内部表示）。字段语义见 §7.1 |
| `AnalysisResult` | 阶段一全篇分析结果。字段语义见 §7.4 |
| `TranslationBatch` | 阶段二翻译批次。字段语义见 §7.2 |
| `TranslationResult` | 单条翻译结果 + 质检状态。字段语义见 §7.3 |

均为普通 `@dataclass`，无方法、无锁：**实例本身不是线程安全的**，跨线程传递依赖信号约定（见架构评审 §3.2）。

---

### 1.2 core.translator — 翻译编排器（三阶段流水线）

职责：把加载→分析→确认→分块→翻译→质检→合并→保存编排为一个可在 QThread 中运行的流水线，并向主线程聚合信号。

**模块级**

| 名称 | 签名 | 说明 |
|---|---|---|
| `_live_threads` | `dict[int, tuple[QThread, TranslatorWorker]]` | 运行中线程注册表，持有强引用防止 QThread 被提前析构。**[GIL 依赖]** |
| `_cleanup_finished_thread` | `(thread: QThread, worker: TranslatorWorker) -> None` | `finished` 信号回调（直连，在垂死的工作线程中执行）：从注册表移除并 `deleteLater` 双方；`RuntimeError`（对象已销毁）吞掉 |

**class `TranslationSignals(QObject)`** — worker 的信号发射器（全部 `pyqtSignal`，跨线程 queued）：

| 信号 | 签名 | 载荷语义 |
|---|---|---|
| `file_started` | `(str)` | filepath |
| `file_analysis_done` | `(str, object)` | filepath, `AnalysisResult` |
| `waiting_for_approval` | `(str)` | filepath — 暂停等待用户确认分析 |
| `analysis_approved` | `(str)` | filepath — 用户已确认 |
| `approval_timed_out` | `(str)` | filepath — 确认等待 300s 超时，继续翻译 |
| `batch_started` | `(str, int, int)` | filepath, batch_id, 总批数 |
| `batch_completed` | `(str, int, object)` | filepath, batch_id, `list[TranslationResult]` |
| `batch_qc_failed` | `(str, int, object)` | filepath, batch_id, `list[TranslationResult]`（已标记 needs_review） |
| `batch_retry` | `(str, int, int)` | filepath, batch_id, retry_num |
| `file_completed` | `(str, str)` | filepath, output_path |
| `file_failed` | `(str, str)` | filepath, error_message |
| `progress_updated` | `(str, int, int, str)` | filepath, current, total, status |
| `all_completed` | `(dict)` | `{filepath: output_path}`，失败文件为空串 |
| `log_message` | `(str)` | 日志文本 |

**class `TranslatorWorker(QObject)`** — 工作线程体

| 成员 | 签名 | 说明 |
|---|---|---|
| `__init__` | `(filepaths: list[str]) -> None` | 初始化信号、取消/继续 `threading.Event`、`ASSTagHandler`。**[主线程]** 创建 |
| `filepaths: list[str]` | 属性 | 该 worker 负责的文件（均分所得） |
| `signals: TranslationSignals` | 属性 | 信号发射器 |
| `waiting_file: str \| None` | 属性 | 当前等待确认的文件；供 facade 路由 `approve_analysis`。worker 写/主线程读，无锁 **[GIL 依赖]** |
| `approve_analysis` | `(edited_analysis: AnalysisResult \| None) -> None` | 确认分析并继续；None=用原始分析。**[主线程]** 调用（置 `_edited_analysis` + `_continue_event.set()`） |
| `run` | `() -> None` | 流水线主体。**[工作线程]**，仅作 `started` 槽连接；单文件异常不中断其余文件（记空串 + `file_failed`）；结束 emit `all_completed`；`finally` 中 `QThread.currentThread().quit()` 保证线程退出 |
| `cancel` | `() -> None` | 置取消事件。**[任意线程]**；在各阶段边界生效 |

`run` 内部可能经信号上报的失败源：`pysubs2.Pysubs2Error`/`UnicodeDecodeError`（加载）、`ValueError`（分析响应无结构）、`OSError`（输出备份失败）、`RuntimeError`（LLM 重试耗尽）——均被 per-file try 捕获转为 `file_failed`，`run` 本身不抛。

**class `TranslatorFacade(QObject)`** — 主线程端编排接口

| 成员 | 签名 | 说明 |
|---|---|---|
| `__init__` | `() -> None` | **[主线程]** 创建 |
| `signals` | `@property -> TranslationSignals` | 聚合信号（转发给 GUI 的唯一入口） |
| `prepare_translation` | `(filepaths: list[str]) -> None` | 按 `parallel_files` 均分文件、创建 N 组 (QThread, worker)、连接全部信号、登记注册表；**不启动线程**。已有线程时强制清理旧线程。**[主线程]** |
| `start` | `() -> None` | 启动全部 worker 线程。**[主线程]** |
| `start_translation` | `(filepaths: list[str]) -> None` | `prepare_translation` + `start` 便捷方法。**[主线程]** |
| `approve_analysis` | `(filepath: str, analysis: AnalysisResult \| None) -> None` | 确认面板当前展示的文件：deepcopy 后路由到匹配 `waiting_file` 的 worker，并推进确认队列；面板展示的不是该文件时忽略（防迟到确认）。**[主线程]** |
| `approve_all` | `(current_filepath: str \| None = None, current_analysis: AnalysisResult \| None = None) -> None` | 当前文件带修改确认，其余等待文件以各自分析（None）确认；清空确认队列。**[主线程]** |
| `cancel` | `() -> None` | 取消全部 worker 并 `_force_cleanup`（quit + wait(3000)）。**[主线程]** |

---

### 1.3 core.subtitle_io — 字幕文件 I/O（pysubs2 封装）

职责：SRT/ASS/SSA/VTT 的加载（多编码回退链）与保存（原子写、保留原样式与时间轴）。

**class `SubtitleFile`**

| 方法 | 签名 | 返回 | 异常 | 线程安全 |
|---|---|---|---|---|
| `load` | `(filepath: str) -> list[SubtitleEntry]` | 条目列表（index=事件行号 0 起；`text`=剥标签 plaintext；`original_text`=含标签原文） | `pysubs2.Pysubs2Error`（空文件/格式不可识别，中文提示）；`UnicodeDecodeError`（编码链全部失败，中文提示） | **[任意线程]** 实例无状态 |
| `save` | `(filepath: str, entries: list[SubtitleEntry], original_filepath: str \| None = None) -> str` | 输出路径。提供 `original_filepath` 时复用其样式/元数据；SRT 输出做 `{\b1}`→`<b>` 修复；条目多于原事件时追加。写盘为 tmp + `os.replace` 原子写 | 解析原文件失败时同 `load`；磁盘 `OSError` 上抛 | **[任意线程]**；同一路径并发写未做互斥（现网无此用法） |

私有成员（勿直接调用）：`_load_any_encoding` / `_decode_chain`（编码回退链：utf-8-sig → utf-8 → UTF-16(带 BOM) → Shift-JIS(含假名校验) → gb18030 → Shift-JIS → latin-1）；`_fix_srt_bold`。

---

### 1.4 core.tag_handler — ASS/SRT 标签处理

职责：格式化标签与文本的分离（占位符化）与还原；判定不可翻译条目。

**class `ASSTagHandler`** — 全部方法 **[任意线程]**（实例无状态）；均不抛异常

| 方法 | 签名 | 返回 |
|---|---|---|
| `extract_tags` | `(text: str) -> tuple[str, dict[str, str]]` | (替换为 `<TAG_N>` 占位符后的文本, `{占位符: 原标签}`)。先 ASS `{\...}` 块再 SRT `<i>/<b>/<u>/<font>` 内联标记 |
| `restore_tags` | `(text: str, tag_map: dict[str, str]) -> str` | 还原占位符为原标签（整串 replace，`<TAG_1>`/`<TAG_10>` 无前缀碰撞） |
| `has_drawing` | `(text: str) -> bool` | 是否含 ASS 绘图块 `{\pN}` |
| `is_skip_entry` | `(text: str) -> bool` | 空行 / 绘图块 / 剥标签后仅音符符号（♪〜…等）→ True（跳过翻译，合并时透传原文） |

---

### 1.5 core.analyzer — 全篇分析器（阶段一）

职责：构建分析提示词（全文/三段取样按 token 预算）、解析 LLM 分析响应。

**模块级**

| 名称 | 签名 | 说明 |
|---|---|---|
| `LANG_NAMES` | `dict[str, str]` | `ja/en/zh-CN/zh-TW` → 中文名（无 `auto` 键） |
| `detect_source_lang` | `(entries: list[SubtitleEntry]) -> str` | 含日文假名 → `"ja"`，否则 `"en"`。**[任意线程]** 纯函数 |

**class `SubtitleAnalyzer`** — 实例方法除注明外 **[任意线程]**（每次调用独立；内部读 AppSettings 与语料库缓存）

| 方法 | 签名 | 返回 | 异常 | 备注 |
|---|---|---|---|---|
| `build_analysis_prompt` | `(entries, source_lang: str, target_lang: str) -> str` | 完整分析提示词 | — | 注入语料库术语（≤60 条）与日文预设术语（≤120 条）；`analysis_mode` 控制全文/智能取样，全文超限自动回退取样 |
| `build_glossary_optimize_prompt` | `(glossary: dict[str, str], entries: list[SubtitleEntry]) -> str` | 术语深度优化提示词 | — | 抽样上限 8000 token |
| `parse_analysis_response` | `(response_text: str) -> AnalysisResult` | 解析结果（部分字段可空） | **`ValueError`** — 完全解析不出 JSON 结构时（防"空面板确认"） | 容错：单条坏分段跳过、glossary 兼容 list/dict、末尾逗号清理（仅解析失败后） |
| `parse_glossary_optimize_response` | `(response_text: str) -> dict[str, str]` | 优化后术语表（失败返回 `{}`） | 不抛 | 静默吞所有解析异常 |

类常量：`FULL_CONTEXT_RATIO=0.7`、`SMART_FULL_RATIO=0.8`、`SAMPLE_BUDGET_RATIO=0.6`。

---

### 1.6 core.chunker — 智能分块器（阶段二前置）

职责：按 token 预算 + overlap + 场景间隔把条目切为 `TranslationBatch` 列表。

**`@dataclass ChunkConfig`**：`target_tokens=2000`、`max_tokens=3000`、`overlap_entries=5`、`min_entries=3`、`max_entries=30`、`break_on_gap_ms=SCENE_BREAK_GAP_MS(5000)`（默认值仅作文档；实际经 `_get_config` 由设置构造，`max_tokens` 钳制为 `min(3000, context_limit//2)` 下限 500）。

**class `SubtitleChunker`**

| 方法 | 签名 | 返回 | 异常 | 线程安全 |
|---|---|---|---|---|
| `chunk` | `(entries: list[SubtitleEntry], analysis: AnalysisResult \| None = None, source_lang: str = "ja") -> list[TranslationBatch]` | 批次列表；每批 `entries` = overlap 前缀 + 主条目（条目为逐字段拷贝）；`estimated_tokens` 含主条目 + overlap + 按语言实测的提示词固定开销 | 不抛（单条超上限只 log.warning 并独占成批） | **[任意线程]**（纯计算；读设置）。`analysis=None` 时话题上下文为空、术语表仅剩语料库强制项 |

---

### 1.7 core.merger — 合并器（收尾）

职责：把 `TranslationResult` 合并回字幕条目并还原标签。

**class `Merger`**

| 方法 | 签名 | 返回 | 异常 | 线程安全 |
|---|---|---|---|---|
| `merge` | `(original_entries: list[SubtitleEntry], all_results: dict[int, TranslationResult]) -> list[SubtitleEntry]` | 合并后的条目（`text`=还原标签后的最终文本）。无结果的条目透传原文；占位符未被还原（大小写不敏感）→ 回退原文**并改写对应 `TranslationResult`**（status=`needs_review`、追加 qc_issues） | 不抛 | **[单 worker 串行]**（无内部状态，但会改写传入的 result 对象——调用方须保证此时 GUI 不再编辑同一对象） |

---

### 1.8 core.qc_checker — 质检器（阶段三）

职责：逐条质检（空译文 / 占位符完整性 / 假名残留 / 译文=原文 / 过短 / 术语一致性）+ 批次放行判定。

**class `QCChecker`**

| 方法 | 签名 | 返回 | 异常 | 线程安全 |
|---|---|---|---|---|
| `check` | `(original_entries: list[SubtitleEntry], translated_texts: list[str], glossary: dict[str, str] \| None = None) -> list[TranslationResult]` | 逐条结果；`status` 为 `qc_passed`/`qc_failed`；长度不一致按短侧截断并 log.warning | 不抛 | **[任意线程]**（无状态） |
| `is_batch_passable` | `(results: list[TranslationResult]) -> bool` | 严重问题（空译文/译文=原文完全相同/缺占位符/多余占位符）占比 ≤20% 且无空译文 | 不抛 | **[任意线程]** |

---

### 1.9 core.corpus_manager — 语料库（强制术语表）管理器

职责：用户强制术语的读写（优先级高于 LLM 分析术语）+ 烤肉圈预设参考术语 + 强制/建议拆分。

文件路径：打包环境 `%APPDATA%/SubtitleTranslator/corpus_vtuber.json`（首运行从 resources 复制）；源码运行 `resources/corpus_vtuber.json`。

**class `CorpusManager`** — 实例 **[任意线程]**；类级缓存 `_terms_cache/_preset_cache` **[GIL 依赖]**

| 方法 | 签名 | 返回 | 异常 | 备注 |
|---|---|---|---|---|
| `get_all` | `() -> dict[str, str]` | 术语表副本 | 不抛 | |
| `get` | `(term: str) -> str \| None` | 单条译法 | 不抛 | |
| `add` | `(term: str, translation: str) -> None` | — | 磁盘 `OSError` 上抛 | 空 key/值忽略；每次写盘（原子写：pid+tid 临时名 + `os.replace` + PermissionError 重试）并同步类级缓存 |
| `remove` | `(term: str) -> None` | — | 同上 | |
| `update_all` | `(terms: dict[str, str]) -> None` | — | 同上 | 批量替换（去空白、滤空） |
| `get_preset_terms` | `@staticmethod () -> dict[str, str]` | 预设参考术语（分类 JSON 拍平） | 不抛（读失败返回 `{}` 并告警） | 类级缓存，进程内只读一次 |
| `resolve_conflicts` | `(llm_glossary: dict[str, str]) -> dict[str, str]` | 语料库版本覆盖 LLM 版本 | 不抛 | |
| `split_terms` | `(llm_glossary: dict[str, str]) -> tuple[dict, dict]` | `(mandatory, suggested)`；语料库全量进 mandatory | 不抛 | `prompt_builder` 每批调用 |

解析失败行为：日志告警 + 本实例空表，**不投毒**类级缓存（下一实例重读盘）。

---

### 1.10 core.extractor — 字幕提取（本地 faster-whisper）

职责：媒体 → 字幕条目/SRT；模型下载进度、缓存、CUDA 回退；QThread worker 编排（与 translator 同范式）。

**模块级函数**

| 函数 | 签名 | 返回 | 异常 | 线程安全 |
|---|---|---|---|---|
| `check_whisper_available` | `() -> str \| None` | None=可用；否则 ImportError 描述 | 不抛 | **[任意线程]**（find_spec 探测，不触发 hub 初始化） |
| `normalize_segment_text` | `(text: str) -> str` | 去首尾空白、换行/全角空格折叠 | 不抛 | **[任意线程]** |
| `segments_to_entries` | `(segments) -> list[SubtitleEntry]` | whisper segments → 条目（毫秒、index 重连续编号、end≤start 补 200ms） | 不抛 | **[任意线程]** |
| `resolve_output_path` | `(filepath: str) -> str` | 同目录 `{stem}.srt`；已存在则备份 `.bak` | `OSError` 可能上抛（replace 失败） | **[任意线程]** |

**`class ExtractionCancelled(Exception)`** — 用户取消提取（分段边界抛出）。

**class `ExtractionSignals(QObject)`**：`extraction_started(str)`、`model_loading(str)`、`extraction_progress(str,int,int,str)`（当前 ms/总 ms/最新文本）、`extraction_completed(str,str,object)`（filepath, srt_path, meta dict）、`extraction_failed(str,str)`、`all_completed(dict)`（`{filepath: srt_path}`，失败空串）。

**class `SubtitleExtractor`** — 模型类级缓存（`_lock` 保护）

| 方法 | 签名 | 返回 | 异常 | 线程安全 |
|---|---|---|---|---|
| `__init__` | `(model_size: str, device: str = "auto", compute_type: str = "auto", cpu_threads: int = 0, hf_mirror: str = "") -> None` | — | — | **[任意线程]**；`hf_mirror` 非空时立即 `apply_hf_mirror`（须在 hub import 前） |
| `from_settings` | `@classmethod () -> SubtitleExtractor` | 按设置构造 | — | **[任意线程]** |
| `extract` | `(filepath: str, language: str \| None, cancel_event: threading.Event \| None = None, vad_filter: bool = True, on_progress=None) -> tuple[list[SubtitleEntry], dict]` | `(entries, meta)`；meta 含 `language/language_probability/duration`。`language=None`=自动检测；`on_progress(当前ms, 总ms, 最新文本)` | **`ExtractionCancelled`**（分段边界）；`RuntimeError`（未识别到语音 / CUDA 缺运行库且无法回退）；下载失败不阻断（转由模型构造兜底） | **[阻塞]**；模型加载有全局锁，推理由调用方串行（现网单 worker）；CUDA 惰性失败自动清缓存 + CPU 重建重试一次 |

**class `ExtractionWorker(QObject)`** — `__init__(filepaths: list[str])`、`cancel()`、`run()`（按 `asr_engine` 设置分派 `OnlineASR`/`SubtitleExtractor`；单文件失败不中断；`finally: quit()`）。与 `TranslatorWorker` 同线程约定。

**class `ExtractionFacade(QObject)`** — `signals` property、`prepare_extraction(filepaths)`、`start()`、`start_extraction(filepaths)`、`cancel()`。全部 **[主线程]**；单 worker 串行识别。内部 `_force_cleanup` 与 translator 同款（cancel → quit → wait(3000)，引用由注册表兜底）。

---

### 1.11 core.asr_online — 在线语音识别（OpenAI 兼容双协议）

职责：`transcriptions`（文件直传，verbose_json 带时间轴）与 `chat_audio`（Chat Completions + input_audio，base64，文本输出按时长均分估算时间轴）两协议；长音频经 `audio_splitter` 自动切块。

**模块级**

| 函数 | 签名 | 说明 |
|---|---|---|
| `check_online_asr_available` | `() -> str \| None` | openai SDK 可用性；None=可用。**[任意线程]** |
| `split_text_evenly` | `(text: str, start_s: float, end_s: float) -> list[SubtitleEntry]` | 无时间轴文本按句切分、时长按字符比例均分（极短句合并、超长句硬拆 ≤25 字）。**[任意线程]** 纯函数 |

**class `OnlineASR`**

| 方法 | 签名 | 返回 | 异常 | 线程安全 |
|---|---|---|---|---|
| `__init__` | `(base_url: str, api_key: str, model: str, protocol: str = "transcriptions", max_upload_mb: float = 25, timeout: int = 600) -> None` | — | **`ValueError`** — 缺 api_key / base_url | **[任意线程]** |
| `from_settings` | `@classmethod () -> OnlineASR` | 按 `ASR_SERVICES` 预设 + 设置构造 | `ValueError` 同上 | **[任意线程]** |
| `validate_api_key` | `() -> bool` | 用 `/models` 轻量验证（不消耗额度） | 不抛（失败返回 False 并告警） | **[阻塞]** 网络调用 |
| `extract` | `(filepath: str, language: str \| None, cancel_event: threading.Event \| None = None, vad_filter: bool = True, on_progress=None) -> tuple[list[SubtitleEntry], dict]` | `(entries, meta)`；meta 含 `language/duration/timeline_estimated`（任一块返回真实分段则整体为真实时间轴）；跨块 index 重编号；临时块自动清理 | **`ExtractionCancelled`**（块边界）；`RuntimeError`（请求失败 / base64 超限 / 无内容） | **[阻塞]**；单请求不可中断（最长 `timeout` 秒）；应用层瞬态重试 3 次（连接/超时/429/5xx，指数退避，429 ×2）；**[单 worker 串行]** |

与本地 `SubtitleExtractor.extract` 同签名，`ExtractionWorker` 按 duck-typing 分派。

---

### 1.12 core.audio_splitter — 音频按时间切块

职责：在线 ASR 单请求大小上限的通用预处理（PyAV 重编码为 mono 16kHz 64kbps mp3 ≈ 8KB/s）。

| 函数 | 签名 | 返回 | 异常 | 线程安全 |
|---|---|---|---|---|
| `probe_duration` | `(path: str) -> float \| None` | 时长（秒）；探测失败 None | 不抛 | **[任意线程]** |
| `iter_audio_chunks` | `(path: str, max_upload_mb: float, direct_formats: tuple[str, ...] = (), tmp_dir: str \| None = None)` | 生成器 `(start_s, end_s, chunk_path)`。体积与格式允许时单块直传原文件（`chunk_path == path`，**不可删**）；否则重编码切块，**临时文件与目录由调用方删除** | 编码 `av` 异常上抛 | **[任意线程]**（每次独立生成器）；**[阻塞]** |

---

### 1.13 core.model_download — whisper 模型下载

职责：HF 模型下载的字节级进度回调 + 本地缓存检测 + 镜像配置。

| 函数 | 签名 | 返回 | 异常 | 线程安全 |
|---|---|---|---|---|
| `model_size_hint` | `(model_size: str) -> str` | 体积提示文案（如"约 1.6GB"） | 不抛 | **[任意线程]** |
| `apply_hf_mirror` | `(hf_mirror: str) -> None` | 设置 `HF_ENDPOINT` + 禁用 Xet。**进程级副作用**，必须在 huggingface_hub 首次 import 前调用 | 不抛 | **[任意线程]**（但时机敏感） |
| `is_model_cached` | `(model_size: str) -> str \| None` | 已缓存→快照路径；否则 None | 不抛 | **[任意线程]** |
| `download_model_with_progress` | `(model_size: str, hf_mirror: str = "", on_progress=None) -> str` | 本地快照目录。`on_progress(done_bytes, total_bytes)` 逐块回调 | **`RuntimeError`**（中文提示：网络/镜像建议） | **[阻塞]**；不可取消；`QThreadPool` 任务中调用（settings 对话框） |

---

## 2. services 包 — LLM 接入层

### 2.1 services.base_llm — 抽象接口 + 统一重试执行器

| 名称 | 签名 | 说明 |
|---|---|---|
| `MAX_RETRY_DELAY` | `= 60.0` | 单次重试等待上限（秒），防服务端 Retry-After 挂死 worker |
| `class DeterministicError(RuntimeError)` | — | 确定性失败（截断/空 choices/解析失败/内容过滤）——`_execute` 不重试直接上抛 |
| `class BaseLLMService(ABC)` | — | 抽象基类，见下 |

**`BaseLLMService` 成员**

| 成员 | 签名 | 说明 |
|---|---|---|
| `translate_batch` | `(batch: TranslationBatch, source_lang: str, target_lang: str) -> list[str]` | 抽象。按输入顺序返回，**仅 primary 部分**（不含 overlap） |
| `analyze` | `(prompt: str) -> str` | 抽象。全篇分析请求，返回响应文本 |
| `validate_api_key` | `() -> bool` | 抽象。验证 Key（实现不得改动全局设置） |
| `provider_name` / `default_model` | `@property @abstractmethod` | 供 GUI 展示 / 测试连接回退 |
| `NON_RETRYABLE_STATUS` | `frozenset({400, 401, 403, 404, 405, 422})` | 确定性失败状态码 |
| `_execute` | `(action: str, fn, limiter: RateLimiter, estimated_tokens: int, backoff_base: float = 2.0) -> T` | 统一执行：`limiter.acquire` 预扣 → `fn()` → 异常时 `refund` 并分流（`DeterministicError` 直抛；不可重试状态码包成 `RuntimeError`；否则指数退避 + 抖动 + Retry-After 优先，封顶 60s）→ 重试耗尽抛 `RuntimeError`。`max_retries = max(1, settings.max_retries)`。**[阻塞]**，等待不响应取消（见架构评审 R5） |
| `_classify_error` | `@classmethod (e: Exception) -> tuple[bool, float \| None]` | duck-typing 读 `status_code`/`response.headers`；429 取 Retry-After（仅数值秒） |

子类需提供 `self.settings`（AppSettings）与 `self.rate_limiter`。

### 2.2 services.rate_limiter — Token 桶限流器

**class `RateLimiter`** — **[任意线程]**（全程持锁记账、锁外等待防队头阻塞）

| 方法 | 签名 | 说明 |
|---|---|---|
| `__init__` | `(tokens_per_minute: int = 90000, requests_per_minute: int = 60) -> None` | 初始半桶（防冷启动突发触发 429） |
| `acquire` | `(estimated_tokens: int = 0) -> None` | **[阻塞]** 直到配额足够；超过桶容量的请求钳制到桶容量（等待上界 ≤60s，绝不无限挂死）；成功时扣 1 请求 + tokens |
| `refund` | `(tokens: float = 0, requests: float = 1) -> None` | 请求失败返还预扣配额（不抛） |

| 函数 | 签名 | 说明 |
|---|---|---|
| `get_rate_limiter` | `(tokens_per_minute: int, requests_per_minute: int) -> RateLimiter` | 按 (tpm, rpm) 的进程级单例（`_limiters_lock` 保护）。OpenAI 路径取 (90000,60)、Claude 取 (60000,30) |

### 2.3 services.prompt_builder — 提示词构建 / 响应解析（双实现共用）

| 函数 | 签名 | 返回 | 异常 | 线程安全 |
|---|---|---|---|---|
| `prompt_overhead_tokens` | `(source_lang: str) -> int` | 按语言实测的系统提示词固定 token 开销（+500 余量），供 chunker 限流记账 | 不抛 | **[任意线程]**；`lru_cache(4)` |
| `build_translation_prompt` | `(batch: TranslationBatch, source_lang: str, target_lang: str) -> str` | 系统提示词：基本规则 + 语言专项指南 + 场景（引号块围栏，防注入）+ 强制/AI 建议/预设参考术语（各自限 40/40/30 条，超量留痕截断） | 不抛 | **[任意线程]**；语料库实例经 `_corpus_lock` 缓存（mtime 签名失效重建） |
| `build_user_message` | `(batch: TranslationBatch) -> str` | `"N. 原文"` 编号列表（**0 起始**） | 不抛 | **[任意线程]** 纯函数 |
| `parse_response` | `(response_text: str, batch: TranslationBatch) -> list[str]` | 解析编号行 → primary 范围译文；兼容 1 起始编号左移对齐、未编号续行拼接、小数前缀/格式噪声行过滤；缺失条目用原文回填 | **`DeterministicError`** — primary 范围内超半数缺失（疑似拒答/截断） | **[任意线程]** 纯函数 |

常量：`LANG_NAMES`、`NUMBERED_LINE_RE`（编号行模式，含 markdown 前缀兼容）。

### 2.4 services.openai_service — OpenAI 兼容实现

**模块级**

| 函数 | 签名 | 说明 |
|---|---|---|
| `is_reasoning_model` | `(model: str) -> bool` | `gpt-5`/`o` 系判定 |
| `chat_completion_kwargs` | `(model: str, temperature: float \| None = None, max_tokens: int \| None = None) -> dict` | 推理模型 → `max_completion_tokens`（下限 16）且不传 temperature；普通模型 → `temperature/max_tokens` |

**class `OpenAIService(BaseLLMService)`** — **[单 worker 串行]**（实例内 `_client` 缓存无锁）

| 方法 | 签名 | 说明 |
|---|---|---|
| `__init__` | `(*, provider: str \| None = None, api_key: str \| None = None, base_url: str \| None = None, model: str \| None = None) -> None` | 显式参数仅供"测试连接"覆盖；None 走全局 AppSettings。`rate_limiter = get_rate_limiter(90000, 60)` |
| `translate_batch` | `(batch, source_lang, target_lang) -> list[str]` | timeout 120s；空 choices / `finish_reason=length` / `content_filter` → `DeterministicError`；经 `_execute`（预扣 `batch.estimated_tokens`） |
| `analyze` | `(prompt: str) -> str` | timeout 90s；`max_tokens=min(analysis_tokens, 8192)`；按固定 `token_limit` 扣限流配额（与 Claude 端实测计费不一致，见 services 审计 P3-6） |
| `validate_api_key` | `() -> bool` | 最小 chat completion（timeout 15s，max_tokens=1）；不抛 |

SDK 客户端 `max_retries=0`（重试策略由 `_execute` 独占）；key/base_url 变化时重建客户端。

### 2.5 services.claude_service — Anthropic 实现

**class `ClaudeService(BaseLLMService)`** — **[单 worker 串行]**

| 方法 | 签名 | 说明 |
|---|---|---|
| `__init__` | `(*, provider, api_key, base_url, model — 同 OpenAIService) -> None` | `rate_limiter = get_rate_limiter(60000, 30)` |
| `translate_batch` | `(batch, source_lang, target_lang) -> list[str]` | system 拆「跨批恒定前缀（cache_control）+ 批级可变」两块（`_split_cached_prompt`，按 `## 当前场景/强制术语/AI 建议术语/预设参考术语` 标题切分）；`max_tokens=8192`；`stop_reason=max_tokens` → `DeterministicError` |
| `analyze` | `(prompt: str) -> str` | 按实测 prompt token 扣限流配额；`max_tokens=min(analysis_tokens, 8192)` |
| `validate_api_key` | `() -> bool` | 同 OpenAI 端模式；不抛 |

静态助手：`_extract_text(response)`（拼接全部 text block，容忍 thinking 块）、`_split_cached_prompt(system_prompt)`。

---

## 3. config 包 — 应用设置

### 3.1 config.settings

模块级私有（勿直接使用）：`_get_data_dir()`、`_dpapi_protect/_dpapi_unprotect`（Windows DPAPI）、`_get_legacy_fernet()`（非 Windows 兜底：持久化随机密钥 0600 文件）、`_legacy_mac_fernet()`（仅读历史格式迁移用）。

**class `AppSettings`** — 单例（`__new__` 实现，`_initialized` 幂等保护）

线程安全：**[主线程]** 为主（QSettings 读写可重入但 `__new__` 无锁；worker 在 `run()` 开始时读一次属 GIL 下可接受用法，见 services 审计 P3-22 / 架构评审 R4）。

| 成员 | 签名 | 说明 |
|---|---|---|
| `get` | `(key: str, default: Any = None) -> Any` | 通用读取（bool 字符串归一化） |
| `set` | `(key: str, value: Any) -> None` | 通用写入（立即持久化到 QSettings） |
| `get_api_key` | `(provider: str) -> str` | 缺失返回 `""`（不抛） |
| `set_api_key` | `(provider: str, key: str) -> None` | 空 key = 删除；立即加密落盘（DPAPI / Fernet，原子写） |
| `get_asr_api_key` / `set_asr_api_key` | 同上，provider 固定 `"asr"` | 在线识别密钥 |
| 数值属性 | 全部经 `_get_number`：非法值回退默认 + [lo,hi] 钳制 | `context_limit(lo≥1000)`、`chunk_tokens(200..100000)`、`analysis_tokens(256..100000)`、`overlap_entries(0..50)`、`temperature(0.0..2.0)`、`max_retries(1..10)`、`max_qc_retries(0..10)`、`parallel_files(1..8)`、`whisper_threads(1..64)`、`asr_timeout(60..3600)`、`bg_opacity(0..100)` |
| 字符串/枚举属性 | 直接 get/set | `provider`、`model`（按 provider 分键 `model_{provider}`）、`analysis_mode`("smart"/"full")、`source_lang`、`target_lang`、`theme`、`bg_color`、`bg_image`、`custom_base_url`（等于默认值时不落盘）、`whisper_model/device/compute/vad`、`extract_lang`、`hf_mirror`、`asr_engine/service/model/base_url` |

默认值与枚举全部来自 `utils.constants`。

---

## 4. utils 包 — 基础设施

### 4.1 utils.constants

常量定义（无函数）。关键分组：`APP_NAME/APP_VERSION/APP_ORG`；`SUPPORTED_FORMATS`（字幕 4 格式）、`MEDIA_FORMATS`（媒体 17 格式）；`SOURCE_LANGUAGES/TARGET_LANGUAGES/LLM_PROVIDERS`；`MODEL_CONTEXT_WINDOWS` + `DEFAULT_PROVIDER_CONTEXT` + `DEFAULT_MODELS` + `DEFAULT_API_BASE_URLS`；`DEFAULT_*` 设置默认值；`SCENE_BREAK_GAP_MS=5000`；`WHISPER_MODELS` + `HF_MIRRORS`；`ASR_ENGINES` + `ASR_SERVICES`（名称/端点/模型列表/协议/单请求 MB 上限）。

### 4.2 utils.logger

| 函数 | 签名 | 说明 |
|---|---|---|
| `setup_logging` | `(log_dir: Path \| None = None) -> logging.Logger` | 返回 `"subtitle_translator"` logger（DEBUG 文件轮转 5×2MB + INFO 控制台；windowed exe 降级链完备：stderr 为 None → NullHandler；目录创建失败 → 仅控制台；`delay=True`）。幂等（已有 handler 直接返回）；**[主线程]** 在 QApplication 前调用一次 |

### 4.3 utils.token_counter

| 名称 | 签名 | 说明 |
|---|---|---|
| `MODEL_ENCODING_MAP` | `dict[str, str]` | 模型 → tiktoken 编码名（gpt-5 系 o200k_base；Claude 用 cl100k_base 近似） |
| `get_encoding` | `(model: str) -> tiktoken.Encoding \| None` | 失败（离线）以 None 缓存短路，回退朴素估算。**[任意线程]**；缓存 dict 无锁 **[GIL 依赖]** |
| `estimate_tokens` | `(text: str, model: str = "gpt-4o") -> int` | tiktoken 计数；回退公式 `max(1, (len-CJK)//4 + CJK)`。**[任意线程]**；`lru_cache(16384)`，注意大文本（全篇 prompt）会占缓存（services 审计 P3-16） |

---

## 5. main.py — 应用入口

| 函数 | 签名 | 说明 |
|---|---|---|
| `main` | `() -> int` | 顺序：预导入 ctranslate2（Qt5 DLL 加载顺序守卫）→ `setup_logging()` → 安装 `sys.excepthook`（windowed exe 异常落日志）→ HighDPI 属性 → QApplication → 延迟 import `MainWindow` → `app.exec_()`。**[主线程]** |

---

## 6. gui 包 — 面板类信号接口与公开方法

> 仅列自定义信号与公开方法/属性；`_` 前缀为内部实现。全部 **[主线程]**。

### MainWindow(QMainWindow)（main_window.py）
- 自定义信号：无（作为组合根连接各面板与 facade 信号）。
- 公开方法：`closeEvent(event)` — 防抖落盘 → 翻译/提取进行中先 `translator.cancel()`/`extractor.cancel()`。
- 关键公开属性：`settings`、`translator: TranslatorFacade`、`extractor: ExtractionFacade`、`theme: ThemeManager`。
- 内部槽组（按 facade 信号一一对应）：`_on_translate/_on_extract`（启动）、`_on_analysis_*`（确认路由）、`_on_batch_*`（预览增量更新）、`_on_cancel`（互斥取消）、`_on_retranslate_entry`（QThreadPool 单条重译，请求时刻锁定归属文件）、`_schedule_auto_save/_do_auto_save/_resave_current_file`（编辑持久化，800ms 防抖）、`_on_save_edits/_on_export/_on_settings/_on_about`、安全看门狗 `_arm_safety_timer/_on_safety_timeout`。

### FilePanel(QWidget)（file_panel.py）
- 信号：`files_changed(list)` — 文件路径列表变化（含拖放/增删）。
- 方法：`add_files(filepaths: list[str]) -> None`（去重追加并勾选）；`get_filepaths() -> list[str]`；`get_checked_filepaths() -> list[str]`；`update_file_status(filepath: str, status: str, progress: str = "") -> None`。
- 事件：`dragEnterEvent/dragMoveEvent/dropEvent/eventFilter`。

### AnalysisPanel(QWidget)（analysis_panel.py）
- 信号：`analysis_modified(object)`（AnalysisResult）；`approve_clicked(object)`；`optimize_glossary(object)`；`approve_all_clicked()`。
- 属性：`analysis -> AnalysisResult | None`（读取时先从 UI 同步回数据对象）；`current_filepath -> str`（面板当前展示的文件，供确认路由）。
- 方法：`show_analysis(filepath: str, analysis: AnalysisResult) -> None`；`clear() -> None`；`on_glossary_optimized(optimized: dict[str, str]) -> None`（深度优化结果回填）。

### PreviewPanel(QWidget)（preview_panel.py）
- 信号：`entry_double_clicked(object)`（TranslationResult）；`retranslate_requested(int)`（条目 index）；`file_selected(str)`（并行多文件切换查看）。
- 方法：`load_entries(filepath: str, entries: list[SubtitleEntry], results: dict[int, TranslationResult] | None = None) -> None`；`set_file_list(filepaths: list[str]) -> None`（多文件时显示下拉）；`update_results(results: list[TranslationResult]) -> None`（批次完成后就地更新受影响行）；`clear() -> None`。
- 表格内编辑产生 `manually_edited` 状态（经由 MainWindow 持有的 results 字典）。

### CorpusPanel(QWidget)（corpus_panel.py）
- 信号：`corpus_changed()`。
- 属性：`corpus -> CorpusManager`。
- 方法：`get_terms() -> dict[str, str]`；`import_from_glossary(glossary: dict[str, str]) -> None`（合并导入，不覆盖已有词条）。

### ProgressWidget(QWidget)（progress_widget.py）
- 无信号。方法：`set_progress(current: int, total: int, status: str = "") -> None`；`set_running(running: bool) -> None`；`reset() -> None`。

### EditorDialog(QDialog)（editor_dialog.py）
- 无信号。`__init__(result: TranslationResult, parent=None)` — **就地修改传入的 result 对象**（保存按钮应用后）；调用方负责刷新预览。

### SettingsDialog(QDialog)（settings_dialog.py）
- 无自定义信号。`__init__(parent=None)`；模态 `exec_()`；确定时才写回设置（外观页交互即时预览项除外）。
- 内部后台任务：模型一键下载（QThreadPool + `_Sig(progress/finished)` 信号回主线程）。
- 注意：`_on_test_connection`/`_on_test_asr_key` 为**主线程同步网络调用**（timeout 15s），见架构评审 R7。

### ThemeManager(QObject)（theme_manager.py）
- 单例。信号：`theme_changed(str)`（"light"/"dark"）。
- 属性：`current_theme -> str`。
- 方法：`apply_theme(theme: str | None = None) -> None`（None=跟随系统）；`apply_custom_background(color: str = "", opacity: int = 100, image_path: str = "", _called_from_clear: bool = False) -> None`；`clear_custom_background() -> None`。

---

## 7. 数据结构字段语义

### 7.1 SubtitleEntry（core/translation_batch.py）

| 字段 | 类型 | 语义 |
|---|---|---|
| `index` | `int` | 条目在**源文件事件序列**中的位置（0 起）。全流水线的唯一寻址键：QC 结果、合并、预览表格、单条重译都按它对齐。提取路径中由 `segments_to_entries`/`asr_online` 重新连续编号（跳过分段不留空洞） |
| `start` / `end` | `float` | 开始/结束时间（**毫秒**；dataclass 注解 float，实际存 int）。源为 pysubs2 事件的 ms 整数或 whisper 秒×1000 |
| `text` | `str` | 当前生效文本。**随阶段变化**：加载后=剥标签 plaintext；标签提取后=含 `<TAG_N>` 占位符的纯文本；merge 后=还原标签的最终文本 |
| `original_text` | `str` | 文件里的原始文本（含全部标签），只读基准；标签还原映射始终从它重新提取（worker 不跨阶段携带 tag_map） |
| `style` | `str` | ASS 样式名（SRT 为空串） |

### 7.2 TranslationBatch（core/translation_batch.py）

| 字段 | 类型 | 语义 |
|---|---|---|
| `batch_id` | `int` | 文件内批次序号（0 起）。GUI 单条重译构造的临时批次用 `-1` |
| `entries` | `list[SubtitleEntry]` | **overlap 上下文条目在前 + 主条目在后**；条目是 chunker 逐字段拷贝的副本（与源列表无共享引用） |
| `primary_start_idx` | `int` | 主条目（真正要翻译并进入结果的条目）在 `entries` 中的起始位置（**含端**） |
| `primary_end_idx` | `int` | 主条目结束位置，**排他端**——语义为切片 `entries[primary_start_idx:primary_end_idx]`。三方一致使用：translator 切片取 primary、`parse_response` 只收 primary 范围译文、GUI 重译单条批次取 `[0:1]`。**不是"最后一条主条目的下标"** |
| `is_overlap_only` | `bool` | 死字段：恒为 `False`（历史遗留，无读取方） |
| `glossary` | `dict[str, str]` | 本批术语表（分析 glossary 的独立副本；语料库强制项由 prompt_builder 运行时叠加，不存于此） |
| `topic_context` | `str` | 当前时段话题描述（可变：QC 重试时把质检问题回灌追加到尾部） |
| `estimated_tokens` | `int` | 限流记账：主条目 + overlap 条目 + 按语言实测的提示词固定开销（`prompt_overhead_tokens`） |

### 7.3 TranslationResult（core/translation_batch.py）

| 字段 | 类型 | 语义 |
|---|---|---|
| `index` | `int` | 对应 `SubtitleEntry.index` |
| `original_text` | `str` | QC 时刻的**剥标签原文**（`entry.text`），非文件原始文本 |
| `translated_text` | `str` | 译文；长度修复/失败回退时等于原文 |
| `status` | `str` | `pending`（默认，实际流水线不产生）/ `qc_passed` / `qc_failed`（QC 单条结论）/ `needs_review`（回退原文、回填、放行批次的遗留问题、标签还原失败等，需人工）/ `manually_edited`（GUI 编辑或单条重译回写，仅 GUI 侧产生）。注意 docstring 未列 `manually_edited`（审计 P3-21 的文档漂移项） |
| `qc_issues` | `list[str]` | 人类可读问题列表；严重问题前缀（`译文为空`/`译文与原文完全相同`/`缺占位符`/`多余占位符`）被 `is_batch_passable` 识别 |

### 7.4 AnalysisResult（core/translation_batch.py）

| 字段 | 类型 | 语义 |
|---|---|---|
| `topic_segments` | `list[dict]` | 话题分段：`{start_idx, end_idx, time_range, topic, key_terms}`。**索引是送给 LLM 的条目原始 index**（跳过绘图/音效后位置≠index，chunker 用 `index_to_pos` 映射对齐；越界/负值被钳制）。仅 `start_idx` 被下游消费；`end_idx` 的开闭语义未定义、未被使用 |
| `speakers` | `list[dict]` | 说话人特征：`{speaker, tone, speech_patterns[]}`（当前未注入翻译提示词，仅供面板展示） |
| `glossary` | `dict[str, str]` | 全局术语表（建议级；语料库项由 `split_terms` 升为强制）。解析兼容 list 形态（source/target/src/tgt/term/translation 键） |
| `style_guide` | `str` | 风格指南（2~4 句；当前未注入翻译提示词，仅供面板展示与确认） |
| `raw_response` | `str` | 原始 LLM 响应（调试用） |

> 使用提醒：`AnalysisResult` 是可变对象，跨线程传递的所有权契约见架构评审 §3.2.1（确认路径 deepcopy、超时路径共享）。
