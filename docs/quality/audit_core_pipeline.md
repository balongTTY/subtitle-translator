# 核心翻译流水线代码审计报告

- **审计对象**: `core/translator.py`（614 行）、`core/translation_batch.py`（52 行）、`core/analyzer.py`（537 行）
- **交叉验证依赖**（为核实数据正确性与并发问题而完整阅读）: `core/chunker.py`、`core/qc_checker.py`、`core/merger.py`、`core/tag_handler.py`、`core/subtitle_io.py`、`core/corpus_manager.py`、`services/base_llm.py`、`services/openai_service.py`、`services/claude_service.py`、`services/rate_limiter.py`、`services/prompt_builder.py`、`config/settings.py`、`gui/main_window.py`、`gui/analysis_panel.py`、`utils/token_counter.py`、`utils/constants.py`
- **运行环境**: Python 3.14.1 + PyQt5 5.15.11（Windows）
- **审计方法**: 三个目标文件逐行阅读；所有并发、序号对齐、占位符、异常路径结论均通过阅读上下游实现交叉验证，不做无依据推断
- **严重度定义**: P0=丢数据/崩溃；P1=功能错误；P2=健壮性缺陷；P3=代码质量

---

## 一、重点核对结论：chunker `primary_end_idx` 语义（审计委托方点名项）

**结论：语义一致，无 off-by-one。**

- `core/chunker.py:145-146`：
  ```python
  primary_start = len(batch_entries)   # 主段在 all_entries 中的起始"位置"
  primary_end = len(all_entries)       # 主段结束"位置"（排他端，Python 切片语义）
  ```
- `core/translator.py:256 / 276`：
  ```python
  primary_entries = batch.entries[batch.primary_start_idx : batch.primary_end_idx]
  ```
- `services/prompt_builder.py:342 / 356` 同样按 `range(primary_start_idx, primary_end_idx)`（排他）使用。

赋值方与两个消费方均按 **排他端（`[start, end)`）** 解释，切片取出的恰好是不含 overlap 的主翻译段。唯一的缺陷是 `translation_batch.py:38` 的注释「新条目结束索引」没有写明排他语义（见问题 #12）。

**同时核验通过的相关项**（未来审计不必重复怀疑）：
- `ASSTagHandler.restore_tags`（tag_handler.py:46-51）用完整占位符 `<TAG_N>`（含右尖括号）做 `str.replace`，`<TAG_1>` 不是 `<TAG_10>` 的子串（`1` 后跟的是 `0` 而非 `>`），**不存在前缀碰撞污染**。
- `Merger.merge`（merger.py:44-58）从 `original_text` 重新提取 tag_map 与 translator.py:140-142 的提取是同函数同算法（计数器每次调用重置），映射确定性一致；残留 `<TAG_*>` 大小写不敏感检测并回退原文，兜底完整。
- `RateLimiter`（rate_limiter.py）全程持锁，`get_rate_limiter` 单例注册亦有锁；`CorpusManager` 类级缓存 + 原子写盘（tmp + `os.replace`）在 GIL 下并发安全。
- `BaseLLMService._execute`（base_llm.py:64-93）有统一重试/指数退避/429 Retry-After/配额返还；`OpenAIService/ClaudeService` 所有请求均显式设置 timeout（120s/90s/15s），不存在无超时的网络调用。
- `TranslatorWorker.run()` finally 中 `QThread.currentThread().quit()`（translator.py:112-116）正确规避了 `started→run` 返回后进入 `exec()` 永久阻塞的问题（`exit()` 先于 `exec()` 时 `exec()` 立即返回，Qt 保证）。

---

## 二、问题清单

### [P1] #1 分析确认超时后，facade 的 `_showing_approval` 永不清除 → 后续文件的确认面板永远不会展示，每个后续文件静默白等 300 秒

- **位置**: `core/translator.py:188-210`（worker 侧超时路径）、`core/translator.py:428-431 / 505-517`（facade 侧队列）
- **问题**: worker 的审批等待最多 300 秒，超时后仅发 `log_message`（translator.py:205），`waiting_file` 置回 `None` 后继续翻译。但 facade 侧没有对应的超时通知：`_showing_approval` 只在 `approve_analysis`（translator.py:539）里被清空。用户未确认时，`_showing_approval` 永远停留在过期文件上，此后所有 worker 的 `waiting_for_approval` 都只会进 `_approval_queue` 排队（translator.py:508-509），`_show_approval` 永不再被调用 → UI 不再收到任何 `waiting_for_approval`/`file_analysis_done`（这两个 UI 信号只在 `_show_approval` 里转发）。多文件批量场景下，第 2 个及以后的文件每个都要等满 300 秒才继续，且界面毫无提示（安全计时器已被 `_on_waiting_approval` 停掉，用户看起来像卡死）。
- **修复建议**: worker 超时路径新增信号通知 facade 推进队列：
  ```python
  # TranslationSignals 新增
  approval_timed_out = pyqtSignal(str)   # filepath

  # translator.py:204-205
  if not approved:
      self.signals.log_message.emit("使用原始分析结果继续翻译（确认超时）")
      self.signals.approval_timed_out.emit(filepath)

  # TranslatorFacade 新增槽并连接 worker.signals.approval_timed_out
  def _on_worker_approval_timed_out(self, filepath: str) -> None:
      self._pending_analysis.pop(filepath, None)
      if self._showing_approval == filepath:
          self._showing_approval = None
          while self._approval_queue:
              nxt = self._approval_queue.pop(0)
              if any(w.waiting_file == nxt for w in self._workers):
                  self._show_approval(nxt)
                  break
  ```
  另外，等待用户确认的 300 秒里安全计时器被停掉，若用户离开，整个批次会无限期挂着——建议把「等待确认」也纳入活动感知看门狗或给 UI 一个明确的倒计时。

### [P1] #2 主窗口关闭时不取消仍在运行的翻译线程，退出阶段可能触发 "QThread: Destroyed while thread is still running" 崩溃

- **位置**: `gui/main_window.py:888-893`（`closeEvent`）；关联 `core/translator.py:32`（`_live_threads` 设计注释）
- **问题**: `closeEvent` 只做了防抖自动保存落盘，没有调用 `self.translator.cancel()`。若翻译进行中（worker 最长阻塞 120s 的 LLM 调用里）用户关闭窗口，`QApplication` 退出、解释器拆卸时会销毁仍在运行线程的 QThread C++ 对象。`_live_threads` 注册表只是把崩溃从「facade 被 GC 时」推迟到「进程退出时」——最终仍会命中 Qt 的硬断言（abort），表现为关闭程序时偶发崩溃/异常退出。
- **修复建议**:
  ```python
  def closeEvent(self, event) -> None:
      if getattr(self, "_auto_save_timer", None) and self._auto_save_timer.isActive():
          self._auto_save_timer.stop()
          self._do_auto_save()
      if self._translating:
          self.translator.cancel()   # 置取消事件 + quit/wait(3s)，未退出的线程仍由注册表兜底
      super().closeEvent(event)
  ```

### [P2] #3 GUI 与工作线程共享同一个 `AnalysisResult` 可变对象，worker 读取时用户面板可继续编辑 → 跨线程可变状态竞争

- **位置**: `core/translator.py:85-88 / 206-209`（`approve_analysis` 直接存引用并在 worker 线程使用）；`gui/analysis_panel.py:219-222 / 542-548`（`analysis` 属性返回面板持有对象、approve 后面板对象不冻结）；`core/translator.py:224-226`（worker 线程遍历 `analysis.glossary`）、`core/chunker.py:63-64 / 193`（worker 线程遍历 `analysis.topic_segments` / `analysis.glossary`）
- **问题**: 用户点「确认」时传给 worker 的是面板 `_analysis` 的**同一引用**；确认后面板并未禁用编辑（approve 只禁用了按钮，术语表单元格/风格文本框仍可编辑，`_sync_from_ui` 会继续写这个对象）。worker 线程正在 `dict(analysis.glossary)`、遍历 `topic_segments`（chunker）时，GUI 线程插入/删除条目，最坏触发 `RuntimeError: dictionary changed size during iteration`（会被 run() 的 per-file try 捕获、整个文件标记失败），一般情形读到中间态（术语表缺条目），且无任何告警。
- **修复建议**: 路由到 worker 前深拷贝，彻底切断共享：
  ```python
  import copy
  # TranslatorFacade.approve_analysis / approve_all
  worker.approve_analysis(copy.deepcopy(analysis))
  # approve_all 中对批量确认的 None 路径无需处理
  ```
  同时建议 approve 后调用 `analysis_panel.set_editable(False)` 之类冻结面板（当前只禁用按钮）。

### [P2] #4 批次翻译的 while 重试循环不检查取消标志，取消后当前批次仍会跑完全部重试

- **位置**: `core/translator.py:244-273`
- **问题**: `_cancel_event` 只在外层 for 循环（translator.py:235）检查。用户取消时，若 worker 正处于某批次的 `while retry_count <= settings.max_qc_retries` 循环，每轮 = 服务层内部重试（base_llm.py：最多 3 次 × 指数退避 sleep + 120s 超时）× translator 层重试（默认 2 次）——最坏可达 9 次 API 调用（约 15~20 分钟）才轮空一次循环条件，期间取消毫无效果，`_force_cleanup` 的 `wait(3000)` 也等不到。
- **修复建议**:
  ```python
  while retry_count <= settings.max_qc_retries and not self._cancel_event.is_set():
      ...
      except Exception as e:
          if self._cancel_event.is_set():
              break                      # 取消立即跳出，不再重试
          ...
  ```
  更彻底的做法是把 `cancel_event` 传入 `BaseLLMService._execute`，在 `time.sleep(delay)` 前后检查，缩短退避等待的取消延迟。

### [P2] #5 通过 20% 阈值放行的批次中，`qc_failed` 条目（含「译文=原文完全相同」的未翻译行）被静默合并落盘，merger 不看 status

- **位置**: `core/translator.py:305-311`、`core/qc_checker.py:133-151`、`core/merger.py:35-45`
- **问题**: `is_batch_passable` 允许 ≤20% 的严重问题条目放行。translator.py:307-310 中这些条目保持 `status="qc_failed"` 却**照样写入 `all_results`**：
  ```python
  for r in batch_results:
      if r.status != "qc_failed" and r.status != "needs_review":
          r.status = "qc_passed"
      all_results[r.index] = r          # qc_failed 也无条件入库
  ```
  merger.merge 完全不检查 `result.status`，直接采用 `translated_text`。后果：「缺占位符」类有 merger 的残留占位符兜底回退原文，但「译文与原文完全相同」（整句没翻）的条目会以原文形态静默进入最终字幕文件，仅在预览里能看到 QC 角标，成品无任何「需审核」痕迹。 translators 侧「回退原文→标 needs_review」的保护（translator.py:254-273、297-303）在 QC 阈值放行路径上是缺失的。
- **修复建议**: 放行批次中的 qc_failed 条目统一降级为 needs_review，并让 merger/UI 感知：
  ```python
  if qc_checker.is_batch_passable(batch_results):
      for r in batch_results:
          if r.status == "qc_failed":
              r.status = "needs_review"
              if "批次放行，但本条存在未解决质检问题" not in r.qc_issues:
                  r.qc_issues.append("批次放行，但本条存在未解决质检问题")
          elif r.status != "needs_review":
              r.status = "qc_passed"
          all_results[r.index] = r
  ```

### [P2] #6 分析阶段：解析失败被吞成「空分析对象」触发空面板确认流程，且分析请求无重试

- **位置**: `core/translator.py:170-183`、`core/analyzer.py:356-406`（`parse_analysis_response` 吞掉 `JSONDecodeError` 等返回残缺对象）、`services/openai_service.py:122 / 145`（`content or ""` 空响应也不报错）
- **问题**: 三种失败形态最终都表现为「成功」：① LLM 返回空串/纯文本无 JSON → `_extract_json` 返回 None → 返回空 `AnalysisResult`；② JSON 截断/损坏 → `_loads_lenient` 抛错被 analyzer.py:403 捕获 → 同样返回空对象；③ 单次网络抖动 → `llm.analyze` 抛错 → 仅 warning 后跳过分析（无重试，对比翻译路径有 3×2 重试）。①② 的返回值不是 None，translator.py:188 判定分析「成功」，照常发 `waiting_for_approval` → 用户看到的是空的话题树/术语表面板，确认后整篇翻译在没有话题上下文与术语（只剩语料库强制项）的情况下进行。数据没有丢，但「分析失败」与「分析为空」不可区分，用户极易在空面板上直接确认。
- **修复建议**: ① `parse_analysis_response` 对「完全解析不出结构」（无 topic_segments 且无 glossary 且无 style_guide）显式 `raise ValueError("分析响应中无有效 JSON 结构")`，让 translator.py:180 的 except 分支接管（跳过分析而非走确认流程）；② 给 `llm.analyze` 增加一次重试（可直接复用 `_execute` 已有的重试，或将 translator 侧包裹一层单次重试）：
  ```python
  try:
      response = llm.analyze(analysis_prompt)
      analysis = analyzer.parse_analysis_response(response)   # 空结构时 raise
  except Exception as e:
      log.warning("全篇分析失败，将跳过分析直接翻译: %s", e)
      analysis = None
  ```

### [P2] #7 长度不匹配时的位置对齐假设 + `parse_response` 1 起始启发式的残余错位风险：序号错位时译文会安装到错误的行上

- **位置**: `core/translator.py:276-291`；关联 `services/prompt_builder.py:305-363`
- **问题**: translator 假设 `translated[i]` ↔ `primary_entries[i]` 一一对应：短了按尾部原文回填、长了截断。这个对齐关系由 `parse_response` 的编号解析保证，但其「1 起始编号整体左移」启发式存在漏判窗口——当 LLM 用 1 起始编号**且恰好丢了最后一行**时（`hit_shift < hit_orig`），启发式不触发，整批译文错位一行（第 i 行装的是第 i-1 条的译文），QC 是逐条检查、检不出跨条错位，错位结果会正常入库落盘。另外 translator.py:280-289 的原文回填逻辑与 `parse_response` 内部的缺号回填（prompt_builder.py:356-361）重复，正常情况下 `len(translated)` 恒等于主段条数，这段代码实际不可达，反而掩盖了「对齐异常」这一应当显式处理的信号。
- **修复建议**: ① `parse_response` 在「primary 范围内命中率过低」（如 `< len(primary)/2`）时抛出异常或返回标记，让上层按批次翻译失败处理（已有重试机制兜底），而不是静默错位；② translator.py:278-291 的长度修复分支改为显式告警+`needs_review` 全批标记，不静默对齐。

### [P2] #8 输出字幕保存是非原子写；且 `_resolve_output_path` 备份失败时仍直接覆盖旧文件

- **位置**: `core/subtitle_io.py:148`（`ssa.save(out_path)` 直写目标路径）；`core/translator.py:374-384`（备份 OSError 后仅告警继续）
- **问题**: 项目里语料库、密钥文件都用了 tmp+`os.replace` 原子写，唯独最终产物字幕文件是直接 `ssa.save`。进程被杀/断电/磁盘满会留下截断损坏的 `_zh` 文件；而此时旧的好文件已被 `out.replace(backup)` 移走（translator.py:375），用户手里只剩坏文件（需手工去翻 .bak）。更差的是备份失败（目标被占用等 OSError）分支：告警后照写，旧的好文件被覆盖且无任何副本。
- **修复建议**:
  ```python
  # subtitle_io.save 末尾
  tmp_path = out_path + ".tmp"
  ssa.save(tmp_path)
  os.replace(tmp_path, out_path)

  # translator._resolve_output_path：备份失败时中止本次保存而不是覆盖
  except OSError as e:
      self.signals.log_message.emit(f"错误: 无法备份旧输出 {out.name}（{e}），本次跳过保存")
      raise OSError(f"输出文件备份失败，已取消写入: {e}") from e
  ```

### [P2] #9 安全超时只恢复 UI 不取消 worker；worker 迟到的 `all_completed` 无代际校验，会污染下一轮结果并可能提前触发收尾

- **位置**: `gui/main_window.py:370-377`（`_on_safety_timeout` 仅恢复按钮）；`core/translator.py:519-526`（`_on_worker_all_completed` 用 `len(self._workers)` 判断收尾）；`core/translator.py:434-446`（`prepare_translation` 重置计数器）
- **问题**: 10 分钟安全超时后 UI 恢复但**旧 worker 未被取消**，继续消耗 API 并会在完成后写盘。用户此时重新开始翻译 → `prepare_translation` 里 `_force_cleanup` 才取消旧 worker（translator.py:437-438），但旧 worker 的 `all_completed({"file": ""})` 信号是排队的，会在新轮 `_completed_workers=0 / _workers=新列表` 之后到达：`_results_accum` 被旧结果污染、`_completed_workers` 虚增；若新轮只有 1 个 worker，虚增后立即满足 `>= len(self._workers)` → **翻译进行中就向 UI 发出 all_completed**，UI 显示「翻译完成 失败:1」并把按钮恢复，与实际状态脱节。
- **修复建议**: ① `_on_safety_timeout` 先 `self.translator.cancel()` 再恢复 UI；② 给 worker 引入代际：
  ```python
  # TranslatorFacade.__init__
  self._generation = 0
  # prepare_translation
  self._generation += 1
  ...
  worker.signals.all_completed.connect(
      lambda results, g=self._generation: self._on_worker_all_completed(results, g)
  )
  # _on_worker_all_completed 首行
  def _on_worker_all_completed(self, results, generation):
      if generation != self._generation:
          return
      ...
  ```

### [P2] #10 `_force_cleanup` 在主线程串行 `wait(3000)` × N 个线程，取消/重开翻译时 UI 最长冻结 N×3 秒

- **位置**: `core/translator.py:597-608`
- **问题**: `parallel_files` 默认 2、可更大。每个 worker 都阻塞在最长 120s 的 LLM 调用里时，`thread.wait(3000)` 必然逐个超时，主线程（GUI）串行等待 2~12 秒，期间界面完全无响应（取消按钮点了没反应的观感最差——恰好是最需要响应的时刻）。
- **修复建议**: 将单线程等待上限压缩并并行化，或完全交给 `_live_threads` 异步回收：
  ```python
  budget_ms = 3000
  running = [t for t in self._threads if t.isRunning()]
  per_thread = max(300, budget_ms // max(1, len(running)))
  for thread in running:
      try:
          thread.wait(per_thread)      # 未退出者由 _live_threads 注册表兜底持有
      except RuntimeError:
          pass
  ```

### [P2] #11 模块级/跨线程共享状态依赖 GIL：`_live_threads`、`waiting_file`、`AppSettings` 单例均无锁——CPython 下安全，Python 3.14 free-threaded 构建下为真实数据竞争

- **位置**: `core/translator.py:32 / 41 / 483`（`_live_threads`：主线程写入、`finished` 直连回调在**工作线程**中 `pop`）；`core/translator.py:83 / 87-88 / 190 / 201 / 534 / 543 / 561-566`（`waiting_file`、`_edited_analysis`：worker 写、主线程读，无同步原语）；`config/settings.py:175-190`（`AppSettings.__new__` 单例无锁，多个 worker 线程在 `run()` 里并发首次调用）
- **问题**: 当前 CPython GIL 构建下这些操作原子、无碍（`finished` 对自由函数的连接是直连，回调确实运行在结束中的工作线程里，`deleteLater` 本身线程安全，dict.pop 亦原子）。但 Python 3.14 提供官方 free-threaded 构建，本项目声明的运行环境恰是 3.14：free-threading 下 `dict` 并发读写、无锁单例双初始化、`_edited_analysis` 的写读重排都是未定义行为级别的数据竞争。此外 `AppSettings.__new__` 的竞态虽因初始化幂等而影响有限（最坏重复加载一次密钥文件），但模式上应修正。
- **修复建议**: 用一把模块级 `threading.Lock` 保护 `_live_threads` 的增删；`waiting_file` 的读写改用 `queue.Queue`/信号传递或 `threading.Lock`；`AppSettings.__new__` 加锁：
  ```python
  _live_threads_lock = threading.Lock()
  # 写入
  with _live_threads_lock:
      _live_threads[id(thread)] = (thread, worker)
  # 回收
  with _live_threads_lock:
      if _live_threads.pop(id(thread), None) is None:
          return
  ```

---

### [P3] #12 `primary_end_idx` 注释未说明排他语义

- **位置**: `core/translation_batch.py:37-38`
- **问题**: 「新条目结束索引」未写明是排他端 `[start, end)`。本次审计已验证 chunker 赋值、translator 切片、parse_response 取值三方一致，但注释歧义极易在后续改动中引入 off-by-one（这正是本次委托重点排查的疑点）。
- **修复建议**:
  ```python
  primary_start_idx: int       # 主条目在 entries 中的起始位置（含端）
  primary_end_idx: int         # 主条目结束位置（排他端，切片语义 entries[start:end]）
  ```

### [P3] #13 `run()` 的 finally 中重复导入 QThread

- **位置**: `core/translator.py:115`
- **问题**: 文件顶部已 `from PyQt5.QtCore import QObject, QThread, pyqtSignal`（translator.py:7），finally 里再次局部导入纯冗余。
- **修复建议**: 直接使用顶部导入的 `QThread.currentThread().quit()`，删除局部 import。

### [P3] #14 审批等待 300 秒为魔法数；QC 重试时 `topic_context` 无上限叠加；网络重试与 QC 重试共用同一计数器

- **位置**: `core/translator.py:194`（`range(300)`）、`core/translator.py:325-328`（`batch.topic_context +=`）、`core/translator.py:244-245 / 317`（`retry_count` 双用途）
- **问题**: ① 300 秒硬编码，与 UI/设置无联动；② 每次 QC 重试都向 `topic_context` 追加一段「特别提醒」，虽受 `max_qc_retries`（默认 2）约束有上界，但建议按轮次替换而非累加；③ `retry_count` 同时承担「LLM 调用失败重试」与「QC 不通过重试」两个预算，一次网络故障会吃掉 QC 重试额度（设计上可行但应注释说明，避免后来者当 bug「修」掉）。
- **修复建议**: 300 提为 `APPROVAL_TIMEOUT_SECONDS` 类常量或设置项；`topic_context` 用「基础值 + 当前轮提醒」组合（保存 base，拼接第 N 轮提醒）；为共用心智补注释。

### [P3] #15 `detect_source_lang` 只区分 ja/en，中文、韩文等源一律按 en 处理

- **位置**: `core/analyzer.py:16-23`
- **问题**: 「自动检测」对无假名的文本（简中、繁中、韩文源字幕）全部返回 "en"，走英文提示词与分析模板；术语参考清单也是英文向。功能可用但检测语义名不副实。
- **修复建议**: 至少补充汉字占比判定（`[一-鿿]` 高占比且无假名 → `zh`）与韩文 `[가-힣]` 判定，或在 UI 上把「自动检测（仅日/英）」写明。

### [P3] #16 分析提示词模板用链式 `str.replace`，字幕正文若含字面 `{corpus_hint}` 会被二次展开

- **位置**: `core/analyzer.py:268-278`
- **问题**: `.replace("{preview}", preview)` 先执行，preview（字幕正文）随后参与后续 `.replace("{corpus_hint}", ...)` / `.replace("{preset_hint}", ...)` 的扫描。字幕文本里恰好出现这些字面量时会被替换成语料库文本（概率极低，但属模板替换的经典陷阱）。
- **修复建议**: 用一次性映射替换或 `str.format`（模板中的字面花括号改双写）：
  ```python
  return (ANALYSIS_PROMPT_JA
          .replace("{preview}", "\x00P\x00")   # 先占位，最后再还原
          ...)
  ```
  或将三个变量全部改为 `format_map` 且保证模板内不含其他裸花括号。

### [P3] #17 `parse_glossary_optimize_response` 静默吞掉所有异常并返回空表

- **位置**: `core/analyzer.py:479-491`
- **问题**: `except Exception: pass` 后返回 `{}`，调用方（main_window.py:645-652）把空 dict 当「优化完成，共 0 个术语」展示，用户无从得知失败原因（对比：术语优化任务里 LLM 异常会以 `_error` 键回传，两条路径行为不一致）。
- **修复建议**: 解析失败时抛出或返回 `{"_error": ...}`，与 main_window 的 `_on_glossary_optimized` 的错误分支对齐。

### [P3] #18 `_clean_json` 的注释与实际行为不符：正则会改写字符串值内部的 `, }` 序列

- **位置**: `core/analyzer.py:532-537`
- **问题**: 注释称「此正则只匹配'前有值的逗号'（不处理字符串内的逗号）」，但 `re.sub(r",\s*([}\]])", r"\1", s)` 是纯文本替换，字符串值内的 `", }"`（如 `"他说, }"`）同样被改写。由于仅在严格解析失败后的兜底路径执行、此时 JSON 本就损坏，实际风险可控，但注释会误导维护者。
- **修复建议**: 修正注释为「可能改写字符串值内的该序列，仅在原文解析失败后作为最后手段使用」；或先尝试用 `json.JSONDecoder().raw_decode` 定位损坏点做最小修复。

### [P3] #19 chunker 话题索引回退值会把「原始索引」误当「过滤后位置」

- **位置**: `core/chunker.py:197-199`（关联发现，直接影响 translator 分批的话题上下文正确性）
- **问题**: `pos = index_to_pos.get(start, start)`——LLM 返回的 `start_idx` 若恰好指向被跳过条目（绘图/音效，分析预览中不存在该行，LLM 偶尔会编造邻近索引）或越界，回退用原始索引当位置使用；跳过项在前面时位置与索引有系统性偏移，`_get_topic_for_range` 取到错误话题。
- **修复建议**: 回退时向下取最近的合法索引而非原值：
  ```python
  pos = index_to_pos.get(start)
  if pos is None:
      lower = [i for i in index_to_pos if i <= start]
      pos = index_to_pos[max(lower)] if lower else 0
  ```

### [P3] #20 每个文件新建 LLM 服务实例（httpx 客户端靠 GC 关闭）；`_get_llm_service` 与 GUI 侧代码重复

- **位置**: `core/translator.py:163 / 402-406`；`gui/main_window.py:654-661`
- **问题**: `_translate_one_file` 每文件构造一个 `OpenAIService/ClaudeService`，其内部 httpx 客户端从未显式 `close()`，依赖 GC 回收（量小、有界，但属未关闭资源）；两处 `_get_llm_service` 逻辑重复，provider 判定改动需改两处。
- **修复建议**: 服务实例提升到 `run()` 级（每 worker 一个，多文件复用同一客户端连接池）；抽公共工厂函数到 `services/__init__.py`。

### [P3] #21 数据类文档漂移：`is_overlap_only` 恒为 False 的死字段；`TranslationResult.status` 注释缺 `manually_edited`

- **位置**: `core/translation_batch.py:39 / 51`
- **问题**: `is_overlap_only` 从未被置 True（chunker.py:173 固定 False），无消费方；`status` 注释列出 5 个值，但 `gui/main_window.py:854` 写入了 `"manually_edited"`，文档与实际状态机不一致。
- **修复建议**: 删除 `is_overlap_only` 或实现其语义；status 注释补 `manually_edited`。

### [P3] #22 `finished` 直连回调在工作线程中执行 `worker.deleteLater()`，此时该线程事件循环已退出，延迟删除事件不可达

- **位置**: `core/translator.py:35-50 / 483-486`
- **问题**: 对自由函数的信号连接是直连，`_cleanup_finished_thread` 在结束中的工作线程里执行。`worker` 的线程亲和是该工作线程，其 `deleteLater` 投递的 DeferredDelete 事件落在一个即将退出/已退出的事件循环里，大概率不被处理；实际回收依赖「thread.deleteLater 在主线程处理 → C++ thread 对象销毁 → 连接与 lambda 释放 → Python GC 回收 worker」这条间接链。功能上兜得住，但链路脆弱且绕。
- **修复建议**: 回调里只做注册表 `pop`；两个 `deleteLater` 改投主线程，例如：
  ```python
  from PyQt5.QtCore import QMetaObject, Qt, Q_ARG
  # 或更简单：在主线程侧用一个常驻的 notifier QObject 的信号转发
  QMetaObject.invokeMethod(main_thread_notifier, "cleanup",
                           Qt.QueuedConnection, Q_ARG("PyQt_PyObject", (thread, worker)))
  ```

---

## 三、Python 3.14 兼容性专项结论

| 检查项 | 结论 |
|---|---|
| 类型注解（`list[str]`、`dict[int, ...]`、`X \| None`） | ✅ 原生泛型/PEP 604，3.9+/3.10+ 即支持，3.14 无问题 |
| `dataclass` + `field(default_factory=...)` | ✅ 无问题（`translation_batch.py` 全部合规，无可变默认值陷阱） |
| `re` / `json` / `threading` / `pathlib` 用法 | ✅ 未使用任何 3.14 移除或行为变更的 API（无 `utcnow`、无废弃 `unittest` 别名等） |
| PyQt5 5.15.11 与 3.14 | ✅ 当前环境已安装并可加载（实测 `pip show pyqt5` = 5.15.11）；无需改动 |
| **free-threaded（no-GIL）构建** | ⚠️ 见问题 #11：`_live_threads`、`waiting_file`、`_edited_analysis`、`AppSettings` 单例、`CorpusManager._terms_cache/_preset_cache`、`token_counter._encoding_cache` 等模块级可变状态均无锁。GIL 构建下全部安全；若部署 free-threaded 3.14，#11 列出的位置需先加锁。tiktoken/pysubs2 等 C 扩展的 free-threaded 兼容性亦需另行验证 |
| PEP 649/749 延迟注解求值 | ✅ 三个目标文件运行时不做注解内省，无影响 |

---

## 四、审计过但确认无问题的高危点（防重复排查）

1. **chunker `primary_end_idx` 排他语义三方一致**（本文第一节，委托方点名项）。
2. **占位符还原无前缀碰撞**：`<TAG_N>` 含右尖括号做整串 replace，`<TAG_1>` 与 `<TAG_10>` 互不为子串（tag_handler.py:46-51）。
3. **审批竞态的「跨文件污染」路径已被 `waiting_file` 匹配守卫**：超时后迟到的确认会因 `waiting_file=None` 匹配失败被丢弃（translator.py:534-547），不会把 A 文件的修改分析误用于 B 文件——真正的缺陷是 #1 的「卡队列」，不是「串文件」。
4. **信号槽线程亲和**：worker 信号 → facade/agg 槽均为跨线程 queued 连接；`AnalysisResult`、`dict`、`list` 作为信号参数传 PyObject 引用是安全的；GUI 侧 `_LoadEntriesTask` 用 pyqtSignal 而非 `QTimer.singleShot(0)` 做跨线程回调（main_window.py:38-44 的注释正确）。
5. **LLM 调用全部有超时**（120s/90s/15s）且有统一重试/退避/429 Retry-After/配额返还（base_llm.py:64-118）；4xx 确定性失败不重试。
6. **限流器与语料库的并发控制正确**：RateLimiter 全程持锁 + 锁外等待防队头阻塞；CorpusManager 原子写盘 + 解析失败不投毒缓存（corpus_manager.py:57-72）。
7. **`_resolve_output_path` 的备份链**（.bak、.bak1、.bak2…）在正常路径下避免了旧翻译被静默覆盖（备份失败分支的覆盖问题见 #8）。
8. **取消语义主体正确**：取消后不 merge/不 save/不报完成（translator.py:340/354、 facade `_cancelled` 拦截迟到 all_completed），不会出现「半成品文件报成功」。

---

## 五、严重度汇总统计

| 严重度 | 数量 | 问题编号 |
|---|---|---|
| **P0**（丢数据/崩溃） | **0** | — |
| **P1**（功能错误） | **2** | #1 审批超时卡死确认队列、#2 退出时线程未取消可致崩溃 |
| **P2**（健壮性缺陷） | **9** | #3 共享 AnalysisResult、#4 重试不检查取消、#5 qc_failed 静默落盘、#6 分析失败吞为空对象、#7 序号错位残余风险、#8 非原子保存+备份失败仍覆盖、#9 迟到 all_completed 无代际校验、#10 取消时 UI 冻结、#11 共享状态无锁（3.14 free-threading） |
| **P3**（代码质量） | **11** | #12 注释歧义、#13 重复导入、#14 魔法数/叠加/共用计数、#15 语言检测二分、#16 模板 replace 顺序、#17 静默吞错、#18 注释与行为不符、#19 话题索引回退偏移、#20 资源复用与代码重复、#21 死字段/文档漂移、#22 deleteLater 不可达 |
| **合计** | **22** | — |

> 优先修复顺序建议：#1、#2（功能/崩溃）→ #5、#6、#8（数据正确性与产物完整性）→ #3、#4、#9（并发健壮性）→ 其余。
