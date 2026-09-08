# 字幕工具 v1 质量工程——修复清单（2026-09-03）

> 本轮为「审计 → 修复 → 验证」全循环的修复侧记录。
> 审计报告见同目录 `audit_*.md`（5 份，共 117 条发现），提示词审查见 `prompt_review.md`，
> 架构评审见 `architecture_review.md`。
> 基线：修复前 60 个单元测试（tests/）；修复后 142 个单元测试 + testfile 各套件全绿。

## 一、已修复问题（按严重度）

### P1（功能错误，会造成数据丢失/必然失败）— 5 项全部修复

| # | 位置 | 问题 | 修复 | 回归测试 |
|---|------|------|------|---------|
| P1-1 | `core/audio_splitter.py` / `core/asr_online.py` | 切块体积规划未计入 base64 ×4/3 膨胀（0.85 系数实际膨胀后超限 13%），MiMo chat_audio 长音频每个满尺寸块必被服务端拒收；运行时护栏又只校验原始字节，拦不住 | 规划系数降至 0.74（0.75 是膨胀临界值，再留请求体余量）；护栏改按 base64 后体积 +1KB 余量校验 | `test_asr_unit.py::TestChunkSizeRegression`（旧系数必挂）+ `TestBase64Guard` |
| P1-2 | `core/asr_online.py` | `input_audio.format` 硬编码 `"mp3"`，wav 直传时声明与数据不符，可能被拒收/误解码 | 按扩展名推导（`.wav`→wav，其余→mp3） | `test_asr_unit.py::TestChatAudioFormat` |
| P1-3 | `core/translator.py` / `core/translator.py`（facade） | 分析确认等待 300s 超时后 facade 的 `_showing_approval` 永不清除，多文件批量时后续文件确认面板永不展示、每个静默白等 5 分钟 | worker 超时路径新增 `approval_timed_out` 信号；facade 收到后清状态并推进确认队列 | `test_translator_pipeline.py::TestApprovalTimeoutFacade` |
| P1-4 | `gui/main_window.py` | `closeEvent` 不取消翻译/提取线程，翻译中关程序退出阶段触发 "QThread: Destroyed while thread is still running" 硬崩溃 | closeEvent 中 `_translating`/`_extracting` 时先 `cancel()` | 手动验证路径（Qt 线程行为，无单测） |
| P1-5 | `services/claude_service.py` / `services/openai_service.py` | 不查 `stop_reason`/`finish_reason`，输出截断时尾部条目静默缺失、被原文填充为「翻译成功」 | 两端加截断检测（max_tokens/length/content_filter/空 choices）→ 抛 `DeterministicError`（不重试直接失败） | `test_services_retry.py::TestTruncationDetection` |
| P1-6 | `core/corpus_manager.py` | 所有实例共用固定临时文件名 `corpus_vtuber.json.tmp`，GUI 与 worker 并发 `_save` 互相截断，`os.replace` 可把半截 JSON 换名成正式文件 → 强制术语库损坏并静默清空 | 临时文件名带 pid+线程 id；Windows 下并发 replace 的瞬时 PermissionError 短暂重试；finally 清理残留 | `test_corpus_manager.py::TestConcurrentSave`（4 线程 × 20 条） |
| P1-7 | `core/subtitle_io.py` | UTF-16（Windows 记事本常见导出）不在编码回退链内，整条回退链中断后报误导性「文件损坏」 | 编码链加入 UTF-16 BOM 检测分支（仅带 BOM 才尝试） | `test_subtitle_io.py::test_load_utf16_with_bom` |

### 提示词工程（prompt_review 高优先级项）

| # | 位置 | 问题 | 修复 |
|---|------|------|------|
| H1 | `services/prompt_builder.py` | 编号起点契约缺失：0 起始从未言明，LLM 的 1 起始重编号必然反复发生、全靠解析启发式兜底（overlap 批部分重编号时存在整批错位一条的洞） | 规则 1 显式声明「编号从 0 开始 + 共 N 行 + 首行必须是 0.」三重锚定，新增 1a 编号纪律；解析启发式平局分支同步收紧（max==条目数才左移） |
| H2 | `core/qc_checker.py` / `core/analyzer.py` / `resources/corpus_vtuber.json` | 术语 value 含「/」备选（来真的/认真）× QC 字面子串匹配 → 必然误报，回灌诱导模型把斜杠字面量写进字幕静默落盘 | QC 按备选分隔符拆分候选、任一命中即通过；分析提示词明示 value 单值化规则；示例与默认语料库 ガチ 条目单值化 |
| A-1b | `core/analyzer.py` | 精修解析失败契约：与「解析成功但结构确实为空」（短字幕合法情形）区分 | 仅解析失败（无 JSON/坏 JSON）抛 ValueError；合法空结构正常走确认面板 |

### P0（崩溃）— 2 项全部修复（GUI 审计发现）

| # | 位置 | 问题 | 修复 | 回归测试 |
|---|------|------|------|---------|
| P0-1 | `gui/preview_panel.py` | 右键菜单引用未定义变量 `idx_item` → NameError，PyQt5 槽内未捕获异常 → 右键即崩、菜单四个功能全不可用 | 删除残留坏行（entry_idx 已由 `_entry_index_at(row)` 求得） | 导入检查 + `testfile/test_preview_parallel.py` |
| P0-2 | `gui/main_window.py` | `closeEvent` 不取消翻译/提取线程，退出阶段析构运行中的 QThread → abort 崩溃 | closeEvent 中先 `translator.cancel()`/`extractor.cancel()`（即上表 P1-4） | 手动验证路径 |

### P1（GUI 功能错误）— 4 项修复

| # | 位置 | 问题 | 修复 |
|---|------|------|------|
| G-1 | `gui/main_window.py` | 安全超时只复位 UI 不取消 worker → 后台继续跑完并写文件，用户重开翻译即双流水线互写输出 | `_on_safety_timeout` 先 `self.translator.cancel()` |
| G-2 | `gui/main_window.py` | `approval_timed_out` 信号 GUI 侧从未连接 → 确认超时后分析面板永久卡在等待态 | 接线 `_on_approval_timed_out`：面板复位、文件状态更新、重新武装安全计时器 |
| G-3 | `gui/main_window.py` | 单条重译回调按回调时刻的 `_current_file` 归属结果 → 并行翻译切换预览文件后结果写进另一文件并落盘（数据串文件） | 请求时刻锁定 `request_filepath`，回调按其归属；仅当前文件刷新预览 |
| G-4 | `gui/main_window.py:329/423` | `conn.disconnect()` 从不生效（PyQt5 的 connect() 返回值无此方法），异常被吞——当前靠每次新建 facade 掩盖，无实际危害 | 记录于审计报告，死代码清理留待后续小修 |
| G-5 | `gui/settings_dialog.py:777/947` | 「测试连接」「测试 Key」在主线程同步发网络请求，超时 15s 内整个 UI 冻结 | 新增 `_validate_in_background`：QThreadPool 后台验证 + 信号回主线程弹结果，按钮期间置灰 |
| G-6 | `gui/main_window.py:733`（配合 `translator._force_cleanup`） | 点「取消」在主线程逐线程 `wait(3000)`，并行任务下 UI 冻结最长 N×3 秒 | `TranslatorFacade.cancel_no_wait()`：跳过阻塞等待，线程由 `_live_threads` 注册表兜底；closeEvent 保持阻塞等待（防解释器拆卸析构运行中线程） |

### P2（健壮性缺陷）— 11 项修复

| # | 位置 | 问题 | 修复 |
|---|------|------|------|
| P2-1 | `core/subtitle_io.py` | pysubs2 保存用 `\r\n` 而解析不剥 `\r`，save→load 回环把 `\r` 污染进字幕文本 | `_decode_chain` 解析前统一换行符为 `\n` |
| P2-2 | `core/merger.py` | 占位符残留检查大小写敏感，LLM 输出 `<tag_0>` 变体时漏网 | `re.search(r"<TAG_\d+>", ..., re.IGNORECASE)` |
| P2-3 | `core/asr_online.py` | API 请求零重试（`max_retries=0` 且应用层无重试），一次网络抖动让已重编码的全部音频块作废 | `_create_with_retry`：瞬态异常（连接/超时/限流/5xx）3 次指数退避重试，限流 ×2 |
| P2-4 | `core/audio_splitter.py` | probe 失败时用输出码率（8400 B/s）估算输入时长，WAV 被估长 20 倍 → 大量空块请求 | 新增 `_input_bitrate` 按输入自身码率估算，兜底才用输出码率 |
| P2-5 | `utils/logger.py` / `main.py` | 日志目录创建失败 → 启动即静默崩溃（windowed exe 无任何可见错误）；无全局异常钩子，未捕获异常静默消失 | logger 全链路 try/except 降级为仅控制台 + `delay=True`；main 安装 `sys.excepthook` 写日志 |
| P2-6 | `core/translator.py` | 批次 while 重试循环不检查取消标志，取消后当前批次仍可跑完 9 次 API 调用（15~20 分钟） | 取消检查进循环条件 + except 分支立即 break |
| P2-7 | `core/translator.py` | 20% 阈值放行批次中的 `qc_failed` 条目（含整句未翻译）以成功形态静默落盘 | 放行批次中 qc_failed 降级 needs_review 并追加「批次放行，但本条存在未解决质检问题」 |
| P2-8 | `core/translator.py` | GUI 与 worker 共享同一 `AnalysisResult` 引用，确认后面板仍可编辑 → 跨线程并发修改 | `approve_analysis`/`approve_all` 路由前 `copy.deepcopy` |
| P2-9 | `core/subtitle_io.py` / `core/translator.py` | 成品字幕非原子直写（断电留截断文件）；备份旧输出失败时仍告警后直接覆盖唯一好翻译 | 保存改 tmp + `os.replace` 原子写（显式传格式标识）；备份失败抛 `OSError` 中止本次保存 |
| P2-10 | `services/base_llm.py` | `_execute` 把解析失败/空 choices 等确定性失败当可重试，整轮重试且每轮真实计费；429 Retry-After 无上限可挂起 worker 小时级；退避无抖动（并发惊群） | 新增 `DeterministicError`（不重试上抛）；Retry-After 封顶 60s；退避加随机抖动 |
| P2-11 | `config/settings.py` | 十余处 `int()/float()` 裸转换：任一配置键损坏（手工编辑 INI/注册表垃圾值）翻译必崩；temperature 无 [0,2] 校验（越界触发 API 400） | `_get_number` 防护读取器（非法回退默认 + 钳制），10 个数值属性全部迁移 |

### P2-补（测试基建）

| # | 位置 | 问题 | 修复 |
|---|------|------|------|
| T-1 | `testfile/test_asr_online.py` | `importlib.reload(core.extractor)` 撤销 monkeypatch 导致运行时 raise 的新类与已导入旧类不匹配 → `except` 失效、组合跑必挂；`SubtitleExtractor.from_settings` patch 后在 patch 之后才保存「原值」；ASR API Key 写入用户 QSettings 不还原 | 改为 patch 前从 `__dict__` 存原始 classmethod、finally 直接恢复；删除 reload；API Key 还原 |
| T-2 | `testfile/test_parser.py` / `tests/test_prompt_builder.py` | 断言「大面积缺条静默回退原文」的旧契约 | 更新为新契约（>50% 缺条抛 `DeterministicError`），补少数缺条回退用例 |

### P2-补（分析链路，core/translator.py + core/analyzer.py）

| # | 位置 | 问题 | 修复 |
|---|------|------|------|
| A-1 | `core/analyzer.py` | 分析响应完全解析不出结构时返回空 `AnalysisResult`，用户面对空面板极易确认 → 整篇翻译无上下文/术语 | 空结构显式 `raise ValueError`，由 translator 的跳过分析分支接管 |
| A-2 | `core/translator.py` | 分析请求零重试，一次抖动就放弃 | 失败重试一次再放弃 |

## 二、新增测试

| 文件 | 数量 | 覆盖 |
|------|------|------|
| `tests/test_merger.py` | 16 | 合并/标签还原/回退/幻觉占位符/越界编号/乱序索引 |
| `tests/test_subtitle_io.py` | 21 | SRT/ASS/VTT 加载、BOM/CRLF、Shift-JIS/GBK 编码链、空文件、保存回写、条目追加、原子写、粗体修复 |
| `tests/test_rate_limiter.py` | 12 | 半桶初始化、容量钳制、等待放行、超容量请求、并发安全、返还、单例 |
| `tests/test_translator_paths.py` | 6 | 输出路径备份链（.bak/.bak1/.bak2…）、备份失败中止、扩展名保留 |
| `tests/test_asr_unit.py` | 7 | wav/mp3 format 推导、base64 护栏、瞬态重试（含限流 ×2 退避）、切块 b64 体积回归 |
| `tests/test_translator_pipeline.py` | 12 | 三阶段流水线端到端（假 LLM）、跳过项透传、LLM 失败回退、取消（前置/批次间/重试中）、坏文件上报、qc_failed 降级、审批超时推进 |
| `tests/test_services_retry.py` | 8 | DeterministicError 不重试、Retry-After 封顶、退避抖动、OpenAI/Claude 截断检测、正常响应不受影响 |
| `tests/test_prompt_builder.py`（增补） | +2 | 多数缺条抛错、少数缺条回退 |

合计：新增/更新 84 个测试。修复过程中被证伪并修正的测试假设 3 处（详见 git history）。

## 三、已记录未修复（低优先级，见各审计报告）

- P3 级 40+ 项（代码质量/文档漂移/平台兼容），详见各 `audit_*.md`
- `core/extractor.py` 单条超长字幕只告警不拆分（core_io P2）——涉及翻译质量策略，建议单独决策
- ASR 取消无法中断进行中的单请求（periphery P2-2 后半）——需 SDK 层支持，改动面大
- testfile 多套件单进程混跑的偶发 Qt 崩溃（既有问题，与本轮改动无关；`run_tests.py` 按进程隔离运行不受影响）
- `_live_threads`/AppSettings 单例无锁——CPython GIL 下安全，free-threaded 构建下为真实竞争（pipeline P2-11）

## 四、验证记录（阶段 5 回归，2026-09-03 02:40）

- `pytest tests/`：**149 passed**（基线 60 → 149，净增 89 个测试）
- `testfile/` 各套件：asr_online 7 ✓、extractor 8 ✓、parser 3 ✓、model_download 5 ✓（pytest）
- 脚本风格套件：test_parallel_gui ✓、test_preview_parallel ✓（直接运行）
- 既有已知问题：testfile 多套件单进程混跑的偶发 Qt 崩溃（与本轮改动无关，stash 对照验证过；分进程运行不受影响）
