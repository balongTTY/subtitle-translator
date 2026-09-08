# 架构评审报告（architecture_review）

> 基线：2026-09-03 源码（含 fix_log.md 记录的全部 P0/P1/P2 修复后的状态）。
> 本文吸收 `audit_core_pipeline.md`、`audit_core_io.md`、`audit_services.md`、`audit_gui.md`、`audit_periphery.md`
> 五份审计中与架构相关的结论，**不重复其逐条问题清单**；本文聚焦分层、耦合、线程模型与重构方向。
> 运行环境：Python 3.14（CPython GIL 构建）+ PyQt5 5.15。

---

## 1. 系统概览

### 1.1 主数据流：翻译流水线（文件 → 保存）

```
 主线程（GUI 事件循环）                                工作线程（QThread × N，N = parallel_files ≤ 8）
┌────────────────────────────────────────────────┐
│ MainWindow（组合根 + 编排接线）                  │
│   FilePanel.files_changed ─► 按钮使能            │
│                                                │
│ _on_translate():                               │
│   facade = TranslatorFacade()   ← 每轮新建      │
│   facade.prepare_translation(files)            │
│     ├ 按 i::n 均分文件 → N 个 TranslatorWorker  │
│     ├ worker.moveToThread(QThread)             │
│     ├ worker.signals.* → facade._agg.* （排队） │
│     └ 登记 _live_threads[id] = (thread,worker)  │
│   连接 facade.signals → MainWindow 槽（14 个）  │
│   facade.start() ═════════════════════════════► QThread.started → worker.run()
│                                                        │  对分到的每个文件串行执行：
│                                                        │  ① SubtitleFile.load()      多编码回退链解析
│                                                        │  ② ASSTagHandler.extract_tags → <TAG_N> 占位符
│                                                        │     is_skip_entry 过滤绘图/音效/空行
│                                                        │  ③ 阶段一 SubtitleAnalyzer：build_analysis_prompt
│   ◄─ file_analysis_done(AnalysisResult 引用) ──────────┤     → llm.analyze() → parse_analysis_response（失败重试 1 次）
│   ◄─ waiting_for_approval ─────────────────────────────┤     → 阻塞等待确认：_continue_event.wait(1)×300
│   ─ approve_analysis(deepcopy 后经 _continue_event) ──►│     （主线程确认 / 300s 超时走 approval_timed_out）
│                                                        │  ④ 阶段二 SubtitleChunker.chunk → [TranslationBatch]
│   ◄─ batch_started / progress_updated ─────────────────┤     对每批循环：
│   ◄─ batch_completed / batch_qc_failed / batch_retry ──┤       llm.translate_batch → BaseLLMService._execute
│                                                        │         （RateLimiter.acquire 预扣 → 调用 → 失败 refund）
│                                                        │       QCChecker.check → is_batch_passable（严重项 ≤20%）
│                                                        │       未过 → issues 回灌 batch.topic_context 重试（≤max_qc_retries）
│                                                        │  ⑤ Merger.merge（标签还原；占位符残留→回退原文+needs_review）
│   ◄─ file_completed / file_failed ─────────────────────┤  ⑥ SubtitleFile.save（tmp + os.replace 原子写）
│   ◄─ all_completed（facade 聚合全部 worker 后 1 次）───┤  finally: QThread.currentThread().quit()
│                                                        │    （run() 是 started 的槽，返回后线程会进入 exec() 永久阻塞，
│ _on_all_completed: 复位 UI / 停安全计时器               │      必须显式 quit 才能退出）
│                                                        │
│ 取消/关窗: facade.cancel()                              │
│   → worker.cancel()（threading.Event）                 │ ◄─ 各阶段边界轮询 _cancel_event，取消后不 merge/不 save/不报完成
│   → quit + wait(3000)（主线程串行，见 §5）              │
│                                                        │ 线程结束后 finished → _cleanup_finished_thread
│                                                        │   （模块级 _live_threads 兜底持有引用，防 QThread 强杀崩溃）
└────────────────────────────────────────────────┘
```

要点：

- **确认流是唯一的 UI↔worker 双向同步点**：worker 用 `threading.Event` 阻塞等待，主线程通过
  `TranslatorFacade.approve_analysis / approve_all` 路由（按 `worker.waiting_file` 匹配）并 `deepcopy`
  切断 GUI 与 worker 的共享（修复 P2-8）。facade 用 `_approval_queue/_showing_approval` 把多文件确认
  **串行化**为「同一时刻只展示一个」，超时路径用 `approval_timed_out` 信号推进队列。
- **facade 每轮翻译新建**（`_on_translate` 里 `TranslatorFacade()`），旧连接靠"整体丢弃旧 facade"失效；
  `_translator_connections` 的 disconnect 代码实际无效（GUI 审计 G-4，记录未修）。
- **安全看门狗**：`_arm_safety_timer`（10 分钟，批次完成时续期，等待确认时暂停）；超时会先
  `translator.cancel()` 再复位 UI（修复 G-1），避免"UI 恢复但 worker 继续写盘"的双流水线竞争。

### 1.2 辅助数据流：字幕提取（媒体 → SRT）与 GUI 内线程池任务

```
 主线程                                    QThread ×1（ExtractionFacade）        QThreadPool.globalInstance()
┌───────────────────────────────┐   ┌─────────────────────────────────────┐   ┌────────────────────────────┐
│ _on_extract()                 │   │ ExtractionWorker.run()              │   │ MainWindow 的 3 类任务：    │
│   ExtractionFacade            │──►│  按 asr_engine 分派：               │   │  _LoadEntriesTask 预览解析  │
│   prepare_extraction(media)   │   │   local  → SubtitleExtractor        │   │  _OptimizeTask   术语优化   │
│   start() ════════════════════►   │   online → OnlineASR（双协议）      │   │  _RetranslateTask 单条重译  │
│  ◄─ extraction_started ───────│◄──│  逐文件 extract()：                 │   │ SettingsDialog 的 1 类任务：│
│  ◄─ model_loading ────────────│   │   audio_splitter.iter_audio_chunks  │   │  _Task 模型下载(_Sig 进度)  │
│  ◄─ extraction_progress ──────│   │   （切块/直传 → 逐块识别 → 拼接）    │   │                            │
│  ◄─ extraction_completed ─────│   │  cancel_event 在分段/块边界生效      │   │ 完成回调统一走 _WorkerDone  │
│  ◄─ all_completed ────────────│   │  finally: quit()；同款注册表清理     │   │ .done 信号（queued 投递）   │
└───────────────────────────────┘   └─────────────────────────────────────┘   └────────────────────────────┘
```

提取与翻译互斥（`_translating/_extracting` 互锁按钮）；提取产物 SRT 自动 `add_files` 回列表可直接翻译。

### 1.3 关键信号槽跨界点清单

| 跨界信号 | 发射方（线程） | 接收方（线程） | 载荷 | 说明 |
|---|---|---|---|---|
| worker.signals.* → facade._agg.* | worker 线程 | 主线程（facade 所在线） | str/int/dict/PyObject 引用 | 全部 queued；PyObject 按引用传递（Qt 审计确认安全） |
| facade._agg.* → MainWindow 槽 | 主线程内转发 | 主线程 | 同上 | 每轮翻译重新连接 |
| waiting_for_approval / approval_timed_out | worker / facade | 主线程 | filepath | 与 approve_* 构成往返协议 |
| _WorkerDone.done | QThreadPool 线程 | 主线程 | tuple/object | 4 处临时 holder 模式（见 §3.3） |
| _Sig.progress/finished（settings） | QThreadPool 线程 | 主线程 | (int,int)/dict | 模型下载进度 |
| CorpusPanel.corpus_changed / PreviewPanel.* 等 | 主线程内 | 主线程内 | — | 纯 UI 内信号，无跨界 |

---

## 2. 分层职责评估

### 2.1 实际依赖方向（按 import 统计）

```
main.py ──► gui ──► core ──► config ──► utils
   │           │  └──► services ──┘
   │           └─────► services（main_window._get_llm_service 直接 import OpenAI/Claude Service）
   │
   └── core ⇄ services 存在【包级循环】：
        core.translator ──► services.openai_service / claude_service / base_llm
        core.chunker    ──► services.prompt_builder（prompt_overhead_tokens 记账）
        services.base_llm / prompt_builder / openai_service / claude_service ──► core.translation_batch
```

### 2.2 各层职责评价

| 层 | 评价 |
|---|---|
| **core** | 职责正确：流水线编排（translator）、纯计算（chunker/analyzer/qc_checker/merger/tag_handler）、I/O（subtitle_io）、ASR 域（extractor/asr_online/audio_splitter/model_download）、共享数据类（translation_batch）。内部用"facade + worker + 注册表"的统一线程范式，两个编排器（translator/extractor）高度对称，是本架构最大的结构性优点。 |
| **services** | 定位为"LLM 接入层"成立：`BaseLLMService` 抽象 + 统一 `_execute` 重试/限流记账 + `RateLimiter` + 双实现 + 共享 prompt_builder。问题在它反过来依赖 core（见 2.3 循环），以及 `prompt_builder` 里混入了限流记账（`prompt_overhead_tokens`）和响应解析（`parse_response`，抛 services 层的 `DeterministicError`）两种关注点。 |
| **gui** | 职责边界总体清楚：面板只做展示与编辑，编排全在 MainWindow；MainWindow 是事实上的组合根 + 编排器（1027 行），除组装外还承担：facade 生命周期管理、确认路由、安全看门狗、单条重译、编辑持久化（自动保存防抖）、导出。这是 GUI 层唯一的"过重"点（见 §4-R6）。 |
| **config** | AppSettings 单例 + QSettings 封装 + DPAPI 密钥存储，位置正确；数值读取统一走 `_get_number` 钳制（修复 P2-11）。依赖方向纯净（只依赖 utils.constants 与 Qt）。 |
| **utils** | 叶子层，无反向依赖，正确。constants 承担了"默认值 + 枚举 + 模型目录"三职，体量偏大但无害。 |

### 2.3 越层与边界异味（不构成缺陷，但影响演化）

1. **core ⇄ services 包级循环**：靠 `core.translation_batch` 是零依赖叶子才没有形成 import 死锁。
   实际依赖链是 `core.translator → services.openai_service → services.prompt_builder → core.translation_batch`。
   三方语义都是"共享数据类"，等价于把 translation_batch 当 shared kernel 使用——能运行，但两个包
   在概念上互为依赖，独立复用/测试 services 层必须拖上 core。
2. **`services.prompt_builder` 直呼 `CorpusManager._resolve_corpus_path()`**（私有方法，代码内有
   AttributeError 兜底）——跨模块私有访问，语料库定位逻辑事实上被两处共享却无公共入口。
3. **GUI 与 core 重复实现两处**：
   - `_get_llm_service()`：`gui/main_window.py` 与 `core/translator.py` 各一份（provider 分派逻辑相同）；
   - 标签还原落盘：`main_window._resave_current_file` 手写"extract_tags → restore_tags → 残留回退原文"
     循环，与 `Merger.merge` 语义重叠（且不含 merge 的 needs_review 标记）。
4. **`main_window` 通过 `Merger().tag_handler` 取标签处理器**（3 处）——为了拿一个无状态工具对象而
   构造域服务，应直接 import `ASSTagHandler`。
5. **worker 内多处函数级延迟 import**（translator.run 里的 `CorpusManager`、`detect_source_lang`；
   extractor 里的 `OnlineASR`、`model_download`）——有的是为规避循环 import，有的是为延迟重依赖
   （faster_whisper），目前未区分动机，新读者难以判断哪些是"必须如此"。

**结论**：依赖方向整体健康（gui→core/services→config/utils 无反向），唯一结构性问题是 core⇄services
循环与两处 GUI/core 重复逻辑；没有 GUI 直接操作 QSettings 之外的越层存储访问。

---

## 3. 耦合点与热点清单

### 3.1 全局可变状态（模块级 / 类级）

| 位置 | 内容 | 并发方 | 现有保护 | 风险评估 |
|---|---|---|---|---|
| `core/translator.py::_live_threads`、`core/extractor.py::_live_threads` | 运行中 QThread 强引用注册表 | 主线程写；finished 直连回调在**垂死的工作线程**中 pop | 无锁；依赖 GIL 原子性 | GIL 构建安全；**free-threaded 3.14 下为真实竞争**（审计 #11） |
| `core/corpus_manager.py::CorpusManager._terms_cache / _preset_cache` | 语料库类级缓存（跨实例复用） | GUI 写、多 worker 读 | 写路径全部经 `_save()`：原子写 + pid/tid 临时名 + PermissionError 重试 + 解析失败不投毒缓存 | 设计完备；残余风险是 `(mtime,size)` 签名可被同签名改写绕过（services 审计 P3-15） |
| `services/rate_limiter.py::_shared_limiters` | (tpm,rpm)→限流器单例 | 全部 worker | `_limiters_lock` 全程持锁；桶操作在锁内、等待在锁外 | **正确的范本**；OpenAI(90000/60) 与 Claude(60000/30) 各得独立桶，参数硬编码（services 审计 P3-10） |
| `config/settings.py::AppSettings._instance` | 设置单例 | 所有线程 | `__new__` 无锁（初始化幂等，最坏重复加载密钥） | GIL 下无害；模式上应加锁（services 审计 P3-22） |
| `services/prompt_builder.py::_corpus_signature/_corpus_instance` | 语料库实例缓存（mtime 签名） | 主线程 + 多 worker | `_corpus_lock` 保护重建 | 并发控制正确；签名机制与私有访问见上 |
| `utils/token_counter.py::_encoding_cache / _fallback_used` | tiktoken 编码器缓存 | 所有线程 | 无锁；dict 赋值 GIL 原子 | free-threaded 下需加锁 |
| `core/extractor.py::SubtitleExtractor._model/_model_key` | whisper 模型类级缓存 | 提取 worker | `_lock` 包住加载/替换/CUDA 回退 | 正确 |
| `gui/theme_manager.py::ThemeManager` | 主题单例 | 主线程 | 仅主线程使用 | 无风险 |
| `core/model_download.py` 的 `HF_ENDPOINT/HF_HUB_DISABLE_XET` 环境变量 | 进程级下载配置 | 提取前设置 | 注释明确"必须在 hub import 前生效" | 隐式全局副作用，跨会话不可回退（periphery 审计 P3-9） |

### 3.2 跨线程共享数据

1. **`AnalysisResult` 所有权不完全对称（最重要的共享点）**
   - worker 生成 → 信号按引用发给 facade（`_pending_analysis` 缓存）→ 面板展示同一对象；
   - **确认路径**已修复：`approve_analysis/approve_all` 先 `copy.deepcopy` 再交还 worker（P2-8）；
   - **超时路径**未拷贝：`approval_timed_out` 后 worker 继续用原对象（构建 forced_glossary、chunker 读
     topic_segments），而面板并未清空、理论上仍可编辑同一对象。窗口小（确认按钮此时已被禁用）、
     但所有权规则不一致——"确认即移交副本、超时即共享"。
2. **`TranslationResult` 对象跨线程共享无拷贝**：worker 把 `batch_results` 的同一批对象既塞进
   `all_results`（稍后 merge 时还会改写 status/qc_issues）又经信号发给 GUI；GUI 把同一批对象存入
   `_current_results` 并在重译/编辑时改写 `translated_text/status`。merge（worker）与 GUI 编辑对同一
   对象的并发写没有同步。实际冲突窗口极小（merge 只在批次全部结束后执行一次，且改写字段不同），
   但契约上"批次结果发射后归谁"没有答案（GUI 审计 #14）。
3. **`TranslationBatch` 共享安全**：chunker 对 entries 逐条深拷贝构造批次（字段级复制），批内
   `glossary=dict(...)` 独立副本——这是做得最干净的一处。QC 重试回灌会原地改 `batch.topic_context`
   （worker 私有，无跨界问题）。
4. **`TranslatorWorker.waiting_file / _edited_analysis`**：worker 写、主线程读，无同步原语；GIL 下
   单引用读写原子，语义依赖"先置 waiting_file 再 emit 信号"的顺序（成立）。
5. **`MainWindow._current_entries/_current_results`**：只被主线程（信号槽与 QThreadPool 回调）读写；
   QThreadPool 任务本身不碰这些容器，只经 `_WorkerDone.done` 把结果投回主线程后处理——方向正确。

### 3.3 信号槽约定与生命周期

- **约定**：跨线程一律默认 AutoConnection（自动 queued）；QThreadPool 回调禁止
  `QTimer.singleShot(0)`（工作线程无事件循环），必须经 `pyqtSignal` 投递——`_WorkerDone` 的
  docstring 已把该约束文档化，全部现网代码遵守（GUI 审计专项核对确认无误）。
- **热点：临时 holder 模式 ×4**（`_LoadEntriesTask`、`_OptimizeTask`、`_RetranslateTask`、settings 的
  `_Task/_Sig`）：`_WorkerDone` 无父对象、局部变量持有，QRunnable `autoDelete=True` 后 queued 事件
  可能在投递与消费之间因 holder 析构被丢弃（GUI 审计 P2-10）。这是同一根因在四处复制，属于
  "缺一个统一的后台任务抽象"的典型症状（§4-R1）。
- **facade 生命周期**：每轮新建 + 重建全部连接；信号断开代码无效但被"整体替换 facade"掩盖。
  迟到信号的防护靠 facade 内部状态（`_cancelled` 拦截 all_completed、`_showing_approval != filepath`
  忽略迟到确认），而非断开连接——约定分散在两处实现里，新人不易看全。
- **GUI 与 worker 的设置一致性**：worker 只在 `run()` 开始时读一次 AppSettings，翻译中途改设置
  不影响进行中任务（但 services 每次调用都读 `self.settings`，实际会读到中途变化，GUI 审计 #25）。

---

## 4. 建议重构项（按 收益/风险 排序，不要求当前实施）

| # | 重构项 | 动机 | 迁移路径 | 收益 | 风险 |
|---|---|---|---|---|---|
| **R1** | **统一后台任务运行时（TaskRunner）** | QThread+Worker+注册表 与 QThreadPool+临时 holder 两套范式并存；holder 生命周期竞态在 4 处复制（§3.3）；模型下载任务不可取消 | 新建 `utils/task_runner.py`：① `run_threaded(fn, on_done)` 内部持 holder 强引用直到回调消费（挂在常驻 QObject 中枢上）；② 保留 QThread 范式给长流水线，但把"注册表 + finished 清理 + quit"收进基类。逐处替换 4 个 QRunnable 调用点 | 高：消除一类静默丢回调竞态；线程退出路径单点可测 | 低：行为保持的重构，`tests/` 142 项可直接回归 |
| **R2** | **明确信号载荷的所有权契约** | `AnalysisResult` 确认/超时两条路径拷贝语义不一致；`TranslationResult` 发射后双主无契约（§3.2.1/2.2） | 约定"**发射即移交**"：worker 发射 `file_analysis_done`/`batch_*` 后不再持有/改写该对象（facade 或 GUI 持唯一所有权）；`merge` 需要改写的结果先按 index 复制。过渡期可先在 `TranslationSignals` docstring 写明契约 | 高：消除全部跨线程可变共享，行为不变 | 低-中：需核对 translator/merger 的写点 |
| **R3** | **打破 core⇄services 包级循环** | services 独立复用/测试被迫拖上 core；`prompt_builder` 混杂记账与解析两种关注点（§2.3.1） | ① `core/translation_batch.py` 明确定位为 shared kernel（无依赖，现状已满足，补文档即可）；② `DeterministicError` 移至 `services/errors.py`（原位置 re-export 过渡）；③ `prompt_overhead_tokens` 移至 `utils/`（token 记账属 utils 语义） | 中：分层语义清晰，依赖单向化 | 低：纯移动 + re-export |
| **R4** | **free-threaded 3.14 预加固** | 项目声明运行于 3.14；`_live_threads`、`AppSettings.__new__`、`token_counter._encoding_cache` 无锁（审计 #11/P3-22） | 三处各加一把模块级 `threading.Lock`（`_live_threads` 的 pop 写法照审计 #11 的示例）；在 README 声明"当前仅支持 GIL 构建"作为短期缓解 | 中：解除未来解释器升级的阻断项 | 低 |
| **R5** | **取消贯通到 services 层** | 取消后正在退避/等限流的 worker 仍最长可挂约 3 分钟（服务重试 3 次 × 退避封顶 60s + 限流等待）；审计 services P3-3 | `_execute(..., cancel_event=None)`、`RateLimiter.acquire(..., cancel_event=None)`；`time.sleep(delay)` 改 `cancel_event.wait(delay)`；`translator` 把 `_cancel_event` 传入 llm 调用 | 中：取消延迟从分钟级降到亚秒级 | 中：改公共签名，需同步测试假 LLM |
| **R6** | **MainWindow 拆分 + 消除 GUI/core 重复** | 1027 行承担编排/持久化/导出多重职责；`_get_llm_service` 与 `_resave_current_file` 与 core 重复（§2.3.3/4） | ① `services/factory.py::create_llm_service(provider=None, ..., settings)` 统一两处工厂；② `_resave_current_file` 改调 `Merger.merge`（获得一致的 needs_review 语义）；③ `Merger().tag_handler` 改为直接 `ASSTagHandler()`；④ 若继续膨胀再抽 `TranslationCoordinator` 控制器（先做 ①②③ 即可，不必一步到位） | 中：消除双实现漂移风险（已发生过：OpenAI/Claude 重试循环漂移的前车之鉴） | 中：GUI 路径无单测，靠手动回归 |
| **R7** | **设置类网络调用移出主线程** | `SettingsDialog._on_test_connection/_on_test_asr_key` 在 UI 线程同步发 15s 超时的网络请求（GUI 审计 P1-7，记录未修） | 复用 R1 的 TaskRunner：`run_threaded(lambda: svc.validate_api_key(), on_done=...)`，按钮期间禁用 | 中：消除最坏 15s×2 的 UI 冻结 | 低 |
| **R8** | **worker 内延迟 import 的动机标注** | 循环规避与重依赖延迟两种动机混用，演化时容易被"顺手上提"破坏（§2.3.5） | 仅为规避循环的延迟 import 加注释 `# 延迟 import：避免 core↔services 循环`；重依赖类（faster_whisper/openai）统一到 `extractor` 现有 try/import 探测函数模式 | 低：防回归 | 低 |

不建议做的事：不建议把 translator/extractor 两个 facade 合并为泛型基类——两者信号协议、确认流、
取消语义差异大于相似度，强行抽象会造出参数化过度的基类；保持当前"对称但不共享代码"的状态即可。

---

## 5. 线程模型专项

### 5.1 全部后台执行单元清单

| 执行单元 | 类型与数量 | 入口 | 取消机制 | 退出路径 | 清理机制 |
|---|---|---|---|---|---|
| **TranslatorWorker**（翻译） | QThread × N（`parallel_files`，1~8；文件均分，文件数 < N 时取小） | `thread.started → worker.run`（queued） | `threading.Event`（`cancel()`）；检查点：每文件开始前、每批次循环条件、每次 API 调用异常后、确认等待（1s 轮询 ×300）。**不可中断点**：单次 LLM 调用（timeout 120s）× 服务层重试（≤3 次，退避封顶 60s+抖动）× QC 重试（≤max_qc_retries） | `run()` 逐文件处理（单文件异常被捕获记为 `""` 并 emit `file_failed`，不中断其他文件）→ emit `all_completed` → `finally: QThread.currentThread().quit()`（防止线程进入 exec() 永久阻塞） | 双保险：① `thread.finished → _cleanup_finished_thread`（模块级函数直连，facade 被 GC 也执行）从 `_live_threads` 移除并 `deleteLater` 双方；② facade `_force_cleanup`：cancel 全部 worker → `requestInterruption()+quit()` → 逐线程 `wait(3000)`；未退出的线程仍被注册表持有，绝不提前释放引用（防 "QThread: Destroyed while thread is still running" 硬崩溃） |
| **ExtractionWorker**（提取） | QThread × 1（whisper 推理本身吃满资源，故意串行） | `thread.started → worker.run` | `threading.Event`；检查点：每文件开始前、每个 whisper 分段 / 每个音频块边界（抛 `ExtractionCancelled`）。**不可中断点**：单次 `transcribe` 调用（`asr_timeout` 默认 600s） | 逐文件 try/except（失败 emit `extraction_failed` 继续）→ `all_completed` → `finally: quit()` | 同 translator：注册表 + `_cleanup_finished_thread`；`ExtractionFacade._force_cleanup` cancel+quit+wait(3000) |
| **_LoadEntriesTask**（预览解析） | QThreadPool 全局池，1 个/文件 | `QThreadPool.start(task)` | **无**（fire-and-forget；与翻译流水线并行解析同一文件，读操作无害） | `run()` 完成 → `holder.done.emit((entries, err))`（queued 投回主线程） | `autoDelete=True`；holder 生命周期见 §3.3 |
| **_OptimizeTask**（术语优化） | QThreadPool，1 个/次点击 | 同上 | **无**（结果含 `_error` 键的错误包，不抛线程异常） | 同上（`_on_glossary_optimized` 恢复按钮） | 同上 |
| **_RetranslateTask**（单条重译） | QThreadPool，1 个/次点击 | 同上 | **无**；归属在**请求时刻**锁定 `request_filepath`（修复 G-3，防并行切换预览后串文件） | 同上；主线程回填结果 → `_schedule_auto_save`（800ms 防抖） | 同上 |
| **_Task（模型下载）**（settings） | QThreadPool，1 个/次点击 | 同上 | **无**（对话框关闭后仍在下载，GUI 审计 P3-21 记录未修） | `finished.emit({"ok": path} / {"error": str})` | 同上（`_Sig` holder） |
| **主线程网络调用** | 无线程——`_on_test_connection` / `_on_test_asr_key` 在 UI 线程同步执行（timeout 15s） | 按钮 clicked | 无 | 同步返回 | — |

### 5.2 线程模型总评

- **退出正确性**：`run()` 作为 `started` 的槽执行后线程会进入 `exec()` 事件循环——两个 worker 都在
  `finally` 里显式 `quit()`，这是 Qt worker 范式最容易漏的点，此处已闭环且有注释。
- **对象生命周期**：模块级 `_live_threads` 注册表是"facade 可能先于线程死亡"这一 Qt 陷阱的解法，
  `finished` 清理用模块级自由函数（非 lambda 捕获 facade 状态），设计自洽；代价是两份几乎相同的
  注册表代码（translator/extractor 各一份，见 R1）。
- **取消粒度**：core 流水线的取消检查点覆盖完整且取消后"不 merge/不 save/不报完成"的语义正确；
  短板集中在 services 层等待不响应取消（R5）与 QThreadPool 任务完全不可取消。
- **主线程阻塞面**：残留两处——设置对话框的同步网络验证（R7）与 `_force_cleanup` 主线程串行
  `wait(3000)×N`（审计 pipeline #10，取消时最坏 N×3s 冻结；改良方向是把 wait 移入完成后回调，
  依赖 R1 的注册表兜底已经安全，只是 UI 响应性优化）。
- **GIL 依赖**：全部跨线程共享的判定（§3.1/3.2）在 CPython GIL 构建下成立；free-threaded 构建是
  显式不支持项（R4），与审计 pipeline §三的结论一致。

---

## 6. 值得保持的设计决策（防"顺手改坏"）

1. **两编排器对称范式**：facade（主线程 API + 信号聚合）+ worker（Event 取消 + run 槽 + finally quit）
   + 注册表兜底——新异步功能应复用此范式而非发明第三种。
2. **限流记账闭环**：预扣 → 失败 refund → 成功不返还；半桶启动防冷启动 429；锁外等待防队头阻塞；
   超容量请求钳制到桶容量防止永久挂死。
3. **`_execute` 统一重试**：确定性失败（`DeterministicError`：截断/空 choices/解析失败）不重试直接上抛；
   429 优先服务端 Retry-After（封顶 60s）；退避带随机抖动防并行惊群；SDK `max_retries=0` 禁用双层重试相乘。
4. **全部落盘原子化**：字幕输出（tmp+replace）、语料库（pid/tid 临时名 + PermissionError 重试）、
   密钥文件（tmp+chmod 0600+replace）。
5. **失败可见性优先**：QC 放行批次的 qc_failed 条目降级 needs_review、原文回填显式标记、
   占位符残留回退原文并标记、超半数缺译文抛 `DeterministicError`——"宁可标需审也不静默成功"是
   全流水线的一致取向，重构时不得弱化。
6. **提示词注入防御三层**：分析产物 `_sanitize_terms`（控制字符/URL 过滤）、`topic_context` 引号块
   包围 + "按数据对待"声明、QC 回灌截断 500 字符加围栏。
