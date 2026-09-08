# GUI 层深度审计报告（gui/ 全部 9 个文件）

- 审计日期：2026-09-03
- 审计范围：`gui/main_window.py`、`gui/settings_dialog.py`、`gui/analysis_panel.py`、`gui/preview_panel.py`、`gui/file_panel.py`、`gui/corpus_panel.py`、`gui/theme_manager.py`、`gui/editor_dialog.py`、`gui/progress_widget.py`（全部逐行读完）
- 参照上下文：`core/translator.py`（TranslatorFacade.cancel/_force_cleanup、approval 队列）、`core/extractor.py`（ExtractionFacade._force_cleanup）、`config/settings.py`（AppSettings 单例 + QSettings 实时读写）、`main.py`（退出路径）
- 验证手段：在本机 PyQt5 5.15.2 上实测 `signal.connect()` 返回 `QMetaObject.Connection`，且该对象**没有** `.disconnect()` 方法（正确 API 是 `bound_signal.disconnect(conn)`），据此确认下文问题 #3。
- 严重度定义：P0=丢数据/崩溃；P1=功能错误；P2=健壮性缺陷；P3=代码质量

---

## 一、逐条问题清单

### [P0-1] preview_panel.py:375 — 右键预览表格必崩（NameError，PyQt5 槽内未捕获异常 → qFatal abort）

`_on_context_menu` 在构建菜单后引用了一个**从未定义**的局部变量 `idx_item`：

```python
entry_idx = self._entry_index_at(row)      # 第 358 行，0-based
if entry_idx is None:
    return
menu = QMenu()
...
entry_idx = int(idx_item.text())           # 第 375 行 — NameError: name 'idx_item' is not defined
```

影响：
1. 用户在预览表格任意行上右键 → NameError。PyQt5 ≥5.5 中，槽函数内未被捕获的 Python 异常默认走 `qFatal()` → `abort()`，**整个应用崩溃退出**（main.py 的 sys.excepthook 只负责记日志，救不了 qFatal）。
2. 右键菜单的四个功能（重译此条/复制原文/复制译文/标记已审核）全部不可用。
3. 即便把 `idx_item` 修出来，这行也是错的：第 0 列显示的是 1 起始序号，`int(idx_item.text())` 得到 1-based，而 `_mark_reviewed` 与 `retranslate_requested` 的接收方（main_window `_on_retranslate_entry`、preview `_mark_reviewed`）都按 0-based `entry.index` 寻址——会稳定地**错一行**。

修复（删除第 375 行，直接复用已算好的 0-based `entry_idx`）：

```python
def _on_context_menu(self, pos) -> None:
    row = self.table.rowAt(pos.y())
    if row < 0:
        return
    entry_idx = self._entry_index_at(row)   # 0-based，保持不变
    if entry_idx is None:
        return
    ...
    orig_text = self.table.item(row, 3).text()
    trans_text = self.table.item(row, 4).text()
    ...
    mark_reviewed.triggered.connect(lambda: self._mark_reviewed(entry_idx))
    retranslate_action.triggered.connect(
        lambda: self.retranslate_requested.emit(entry_idx))
    # 删除: entry_idx = int(idx_item.text())
```

加固建议：给关键槽加 try/except 或在 main.py 用 `app.exec_()` 前安装 `sys.excepthook` 之外，再用 `QApplication` 的通知机制把槽异常降级为提示而非 abort（PyQt5 没有 official 开关，实践中靠槽内防御）。

---

### [P0-2] main_window.py:888-893 — closeEvent 不停止后台线程：退出时崩溃 / 输出文件写一半

```python
def closeEvent(self, event) -> None:
    if getattr(self, "_auto_save_timer", None) and self._auto_save_timer.isActive():
        self._auto_save_timer.stop()
        self._do_auto_save()
    super().closeEvent(event)     # 之后再无任何线程收尾
```

翻译或提取进行中用户点窗口 X：
1. 主窗口关闭 → 最后一个窗口关闭 → `app.exec_()` 返回 → 解释器 shutdown。此时 worker QThread 仍在跑（worker 阻塞在最长 120s 的 LLM 调用或 whisper 推理里）。解释器终结阶段模块级 `_live_threads` 注册表被清空，仍运行的 QThread C++ 对象被析构 → `QThread: Destroyed while thread is still running` → `qFatal/abort` 硬崩溃；即使侥幸不崩，CPython finalization 与还在执行 Python 代码的原生线程竞态也可能段错误。
2. 若 worker 正处于 `sub_file.save()`（translator.py:368 / extractor.py:383）写 `_zh` 输出文件的窗口期，进程被杀 → **输出字幕文件截断，直接丢数据**。
3. `TranslatorFacade.cancel()` 与 `_force_cleanup()`（translator.py:605-645，已实现"置 cancel_event → quit → wait(3000) → 注册表兜底"的完整安全路径）以及 `ExtractionFacade.cancel()` **在 closeEvent 里完全没有被调用**。现有基础设施形同虚设。

修复：

```python
def closeEvent(self, event) -> None:
    if getattr(self, "_auto_save_timer", None) and self._auto_save_timer.isActive():
        self._auto_save_timer.stop()
        self._do_auto_save()

    if self._translating or self._extracting:
        ret = QMessageBox.question(
            self, "任务进行中",
            "翻译/提取尚未完成，退出将中止当前任务。\n确定退出吗？",
        )
        if ret != QMessageBox.Yes:
            event.ignore()
            return
        if self._translating:
            self.translator.cancel()   # 复用已有 _force_cleanup
        if self._extracting:
            self.extractor.cancel()
    super().closeEvent(event)
```

注意残留风险：`_force_cleanup` 的 `wait(3000)` 超时后线程可能仍在 API 调用里。translator worker 在每个检查点（analyze 后、每批循环头、merge 前，translator.py:186/241/357）检查 `cancel_event`，取消后**不会**再落盘，所以等 3000ms 已排除"写一半"的主要风险；若要绝对杜绝退出阶段 abort，可进一步 `event.ignore()` 后等 `QThread.finished`（经 `_cleanup_finished_thread`）再真正 close，或对 `_live_threads` 里未退出的线程 `thread.wait()`（无超时）阻塞在退出前。

---

### [P1-3] main_window.py:329-334、423-428 — "断开旧信号"从不生效（connect() 返回值没有 disconnect 方法）

```python
self._translator_connections = [
    s.file_started.connect(self._on_file_started),
    ...
]
...
for conn in self._translator_connections:
    try:
        conn.disconnect()          # AttributeError，被下一行吞掉
    except Exception:
        pass
```

实测（PyQt5 5.15.2）：`signal.connect(slot)` 返回 `PyQt5.QtCore.QMetaObject.Connection`，该类型**没有** `.disconnect()` 成员 → 每次都抛 `AttributeError` → 被 `except Exception: pass` 静默吞掉。**旧信号连接从未被断开**。

当前没有立刻爆炸，是因为 `_on_translate`/`_on_extract` 每次都 new 一个新 facade、旧 facade 被 GC 时其信号连接自动销毁，掩盖了这段死代码。但它是隐患：
- 一旦有人复用 facade（或 facade 因引用被持有而存活），同一槽会被连接两次，信号触发时槽执行两遍（UI 状态双写、状态栏闪烁错乱）。
- 这段"防御代码"给人虚假的安全感。

修复（三选一）：
1. 直接删除这两段死代码（每次新建 facade 的设计下本来就不需要手工断开）；
2. 改为正确的 API：保存 `(signal, conn)` 对，断开时 `sig.disconnect(conn)`；
3. 保存槽列表，断开时 `sig.disconnect(slot)`。

```python
# 方案 2 示例
self._translator_connections = [
    (s.file_started, s.file_started.connect(self._on_file_started)),
    ...
]
for sig, conn in self._translator_connections:
    try:
        sig.disconnect(conn)
    except (TypeError, RuntimeError):
        pass
```

---

### [P1-4] main_window.py:360-377 — 安全超时只"恢复 UI"不取消 worker，且 10 分钟阈值小于最坏单批耗时

`_on_safety_timeout` 触发时只重置 `_translating`/按钮/进度条，**没有调用 `self.translator.cancel()`**：

```python
def _on_safety_timeout(self) -> None:
    if self.translate_btn and not self.translate_btn.isEnabled():
        log.warning("翻译超时——10 分钟无批次完成，强制恢复 UI")
        self._translating = False
        ...
```

两个问题：
1. 后台 worker 继续全速跑完并**正常写 `_zh` 输出文件**（没有 cancel_event，translator.py 的各检查点全数通过）。UI 却显示"翻译超时，请检查网络和 API 配置后重试"，用户很可能重新点"开始翻译"——于是旧流水线仍在写同一批文件的 `_zh` 输出、新流水线也在写，输出文件互相顶掉（虽有 `.bak` 备份兜底），API 配额双倍消耗。
2. 10 分钟阈值本身会**误伤正常任务**：单批最坏耗时 = 服务层重试(max_retries≤10) × LLM 超时 + QC 重试(max_qc_retries≤5)，单文件多批场景下 10 分钟无 `batch_started` 完全可能发生在一次超长 LLM 调用内——此时 UI 已被强制恢复而翻译其实还活着，与用户认知脱节。

修复：

```python
def _on_safety_timeout(self) -> None:
    if self.translate_btn and not self.translate_btn.isEnabled():
        log.warning("翻译超时——10 分钟无批次完成，取消任务并恢复 UI")
        self.translator.cancel()          # 关键：让 worker 停下来
        self._translating = False
        self.progress_widget.set_running(False)
        self.translate_btn.setEnabled(True)
        self._on_files_changed(self.file_panel.get_filepaths())
        self.status_bar.showMessage("翻译超时已中止，请检查网络和 API 配置后重试")
```

并把阈值放宽（如 20 分钟）或改为"两次 batch 信号之间的间隔"计时。

---

### [P1-5] main_window.py:336-352 + analysis_panel — approval 超时后 GUI 侧无任何联动，确认面板永久卡在"等待确认"状态

信号链路（facade 侧 `_showing_approval` 清理正在修复，此处只审 GUI 侧联动）：
- worker 确认等待 300 秒超时 → 发 `approval_timed_out`（translator.py:211）。
- facade 的 `_on_worker_approval_timed_out`（translator.py:532）只清理内部队列，**不向 `_agg` 转发该信号**。
- main_window `_on_translate` 连接的 13 个信号里**根本没有 `approval_timed_out`**（main_window.py:338-352）。

GUI 侧后果（facade 修复后依然存在）：
- 超时后 worker 按原始分析继续翻译，但 `AnalysisPanel` 仍显示蓝色等待条"翻译已暂停 — 请检查并修改分析结果…"，`approve_btn`/`skip_btn`/`approve_all_btn` 全部可点；
- 用户此时点"确认继续翻译" → `facade.approve_analysis` 因 `_showing_approval` 已清空而**静默忽略**（translator.py:563-564）→ 用户以为确认成功，实际什么都没发生；
- 单文件场景（最常见）下面板从此永远停在等待态，直到翻译结束；文件行状态也会先停在"待确认分析"直到 `batch_started` 才被纠正。

修复（GUI 侧三步 + 依赖 facade 一行改动）：

facade 侧（配套，一行）：在 `_on_worker_approval_timed_out` 末尾补 `self._agg.approval_timed_out.emit(filepath)`。

main_window 侧：

```python
# _on_translate 的信号列表里追加
s.approval_timed_out.connect(self._on_approval_timed_out)

def _on_approval_timed_out(self, filepath: str) -> None:
    if self.analysis_panel.current_filepath == filepath:
        self.analysis_panel.on_approval_timed_out()
    self.status_bar.showMessage(
        f"{Path(filepath).name} 分析确认超时，已按原始分析结果继续翻译")
```

analysis_panel 侧新增：

```python
def on_approval_timed_out(self) -> None:
    """确认等待超时：worker 已按原始分析继续，复位面板等待态"""
    self.approve_btn.setEnabled(False)
    self.skip_btn.setEnabled(False)
    self.approve_all_btn.setEnabled(False)
    self.waiting_hint.setText("确认等待超时，已按原始分析结果继续翻译（修改未生效）")
    self._update_waiting_hint_style()
```

---

### [P1-6] main_window.py:800-832、834-869 — 单条重译结果按"回调时刻"的当前文件归属，并行切换预览时结果串文件

`_on_retranslate_entry` 发起后台重译时，lambda 只捕获了 `index`，没有捕获发起时的文件路径：

```python
holder.done.connect(
    lambda payload: self._on_entry_retranslated(index, payload[0], payload[1])
)
```

`_on_entry_retranslated` 回调里用 `self._current_file` 取 entries/results：

```python
entries = self._current_entries.get(self._current_file, [])
...
results = self._current_results.setdefault(self._current_file, {})
```

单条 LLM 重译耗时以十秒计。期间用户在预览面板顶部的文件下拉里切到另一个并行文件（`_on_preview_file_selected` 会改 `_current_file`）→ 重译结果被写进**另一个文件**的同号条目：覆盖人家已有的译文、污染其 QC 状态，且随后自动保存会把这个错误写进另一文件的 `_zh` 磁盘文件。同理 `_schedule_auto_save`/`_do_auto_save` 也按 `_current_file` 保存，可能把重译结果存错文件。

修复（把文件路径随闭包一起捕获）：

```python
src_filepath = self._current_file
holder.done.connect(
    lambda payload, fp=src_filepath:
        self._on_entry_retranslated(fp, index, payload[0], payload[1])
)

def _on_entry_retranslated(self, filepath: str, index: int,
                           text: str | None, err: str | None) -> None:
    ...
    entries = self._current_entries.get(filepath, [])
    results = self._current_results.setdefault(filepath, {})
```

`_do_auto_save` 亦应携带 filepath（如 `self._auto_save_file = src_filepath`，到点保存该文件而非"当前显示的文件"）。

---

### [P1-7] settings_dialog.py:947-990、777-801 — "测试连接"/"测试 Key"在主线程同步发网络请求，UI 整体冻结

`_on_test_connection` 直接在点击槽里调用 `svc.validate_api_key()`；`_on_test_asr_key` 同样直接调用 `asr.validate_api_key()`。这两个都是真实 HTTP 请求（未验证的 Key/错误端点还要等 TLS 握手失败或服务端超时，叠加服务层重试可达数十秒）。期间：
- 整个 GUI 冻结（包括"取消测试"的可能——根本没提供取消按钮）；
- Windows 下窗口变白、"未响应"，用户大概率强杀进程；
- 模态设置对话框也无法关闭。

设置对话框里 `_on_download_model` 已经示范了正确模式（QRunnable + pyqtSignal 回主线程），测试连接应复用同一模板：

```python
def _on_test_connection(self) -> None:
    ...
    self.test_btn.setEnabled(False)
    self.test_btn.setText("测试中…")
    holder = _TestSig()                       # QObject + pyqtSignal(object)
    holder.done.connect(self._on_test_finished)
    QThreadPool.globalInstance().start(_TestTask(svc, holder))

def _on_test_finished(self, result: dict) -> None:
    self.test_btn.setEnabled(True)
    self.test_btn.setText("测试连接")
    ...  # 按 result 弹窗
```

---

### [P1-8] main_window.py:718-732（配合 translator.py:638-643 / extractor.py:461）— 点"取消"在主线程逐线程 wait(3000)，并行任务下 UI 冻结最长 8×3s

`_on_cancel` 在主线程槽里直接调用 `self.translator.cancel()` / `self.extractor.cancel()`，两者最终都会走到 `_force_cleanup()` 里的：

```python
for thread in self._threads:
    if thread.isRunning():
        thread.wait(3000)      # 每线程最长阻塞主线程 3 秒，串行
```

取消时 worker 大概率正阻塞在 LLM 调用（最长 120s）或 whisper 推理里，`wait(3000)` 必然超时。`parallel_files` 最大 8 → 翻译取消最坏 **8 × 3s = 24 秒**主线程冻结，且发生在用户刚点了"取消"、正期待界面响应的时刻——观感等同"程序挂了"，用户强杀则回到 P0-2 的场景。

修复方向（任选或组合）：
1. `_on_cancel` 槽里只做 `worker.cancel()`（置事件，非阻塞），立即恢复 UI；把 `quit+wait` 的 `_force_cleanup` 挪到一次性后台线程或 `QTimer.singleShot(0, ...)` 后的空闲期执行；
2. 或 facade 增加轻量 `cancel_async()`：`requestInterruption()+quit()` + 连接 `finished` → `_cleanup_finished_thread`（注册表已保证安全回收），完全不 wait；
3. 若保留 wait，至少把进度反馈给用户（光标改忙、状态栏提示"正在等待工作线程退出…"）。

---

### [P2-9] analysis_panel.py:229-241 — show_analysis 不恢复 approve_all_btn，并行多文件时"确认全部"永久禁用

`_on_approve_all`（:556-563）把 `approve_all_btn` 置灰；`show_analysis` 只恢复：

```python
self.approve_btn.setEnabled(True)
self.skip_btn.setEnabled(True)
# 缺: self.approve_all_btn.setEnabled(True)
```

并行多文件场景：确认完文件 A → 队列展示文件 B 的分析 → B 的"确认全部"按钮仍是灰的，只能逐个确认，功能静默失效。

修复：`show_analysis` 中补一行 `self.approve_all_btn.setEnabled(True)`。（`_on_approve`/`_on_skip` 路径不受影响，因为它们不禁用 approve_all_btn。）

---

### [P2-10] main_window.py:520-546（及 :610、:800、settings_dialog.py:750-758）— QRunnable 里临时 `_WorkerDone` holder 的生命周期竞态，queued 回调可能被静默丢弃

以 `_on_file_started` 为例：

```python
holder = _WorkerDone()                        # 无父 QObject，局部变量
holder.done.connect(lambda payload: ...)
...
QThreadPool.globalInstance().start(_LoadEntriesTask(filepath, holder))
```

`QThreadPool` 默认 `autoDelete=True`：`run()` 返回（`done.emit` 刚把 queued 事件投递到主线程队列）后 C++ 侧立即删除 QRunnable → Python 包装器销毁 → `holder` 引用计数归零 → 无父 QObject 被析构 → Qt 清除该对象所有待处理事件。若主线程此刻繁忙（恰好在处理其他 GUI 事件），**已投递的回调被丢弃**：`_current_entries[filepath]` 永远停留在空占位 `[]`，该文件翻译期间预览一直空白/不更新，直到 `file_completed` 兜底同步解析才自愈。`_OptimizeTask`、`_RetranslateTask`、settings 的 `_Task` 是同一模式的四份拷贝。

修复：让存活到回调之后的对象持有 holder 引用（最简单是挂在 self 上，用完移除）：

```python
if not hasattr(self, "_pending_holders"):
    self._pending_holders = []
holder = _WorkerDone()
self._pending_holders.append(holder)
holder.done.connect(
    lambda payload, h=holder, fp=filepath: self._on_entries_loaded(fp, payload[0], payload[1])
)
# _on_entries_loaded 开头: self._pending_holders.remove(...)  或统一在 done 里 discard
```

更优解：`_WorkerDone` 提升为 MainWindow 的常驻成员（一个信号中枢复用于全部 QRunnable 回调）。

---

### [P2-11] preview_panel.py:325-342 — 编辑"尚未翻译"行的译文被静默丢弃

```python
def _on_cell_changed(self, row: int, col: int) -> None:
    if col != 4:
        return
    entry_idx = self._entry_index_at(row)
    ...
    new_text = self.table.item(row, 4).text()
    if entry_idx in self._results:
        self._results[entry_idx].translated_text = new_text
        ...
    # 没有 else：不在 results 里的条目，编辑只活在 QTableWidgetItem 里
```

批次结果尚未到达（状态 `[⏳]`）或条目被过滤视图排除过时，用户在译文列输入的内容不会进入 `self._results`；下一次 `_populate_table()`（切筛选、`update_results` 回退重建、切文件）就把编辑**无提示冲掉**，且 `_resave_current_file`（按 `_current_results` 合并）也永远不会保存它。

修复：else 分支落一个 `TranslationResult`：

```python
else:
    entry = next((e for e in self._entries if e.index == entry_idx), None)
    self._results[entry_idx] = TranslationResult(
        index=entry_idx,
        original_text=entry.original_text if entry else "",
        translated_text=new_text,
        status="manually_edited",
    )
    status_item = self.table.item(row, 5)
    if status_item:
        status_item.setText("[✎]")
```

---

### [P2-12] settings_dialog.py:478-493、512-519 — 外观设置在交互瞬间即持久化，点"取消"不回滚

`_pick_bg_color → _set_bg_color` 里直接 `self.settings.bg_color = hex_color`（AppSettings 属性 setter 立即写 QSettings 落盘）；`_reset_appearance` 同样直接写 `bg_opacity`/`bg_image`。用户选完颜色后点对话框"取消"（`reject`）——背景色已经永久保存，下次启动生效，与"取消"语义相悖。其他所有设置项都是 `_on_ok` 时统一落盘，仅外观是即时写。

修复：对话框内用临时状态，`_on_ok` 才写入：

```python
def __init__(self, parent=None):
    ...
    self._tmp_bg_color = self.settings.bg_color   # 临时值

def _set_bg_color(self, hex_color: str) -> None:
    self._tmp_bg_color = hex_color                # 只改临时值 + 更新按钮色块
    ...

def _on_ok(self) -> None:
    ...
    s.bg_color = self._tmp_bg_color               # 此刻才落盘
```

`_preview_appearance`/`_reset_appearance` 的即时预览可保留（只动 QApplication 样式，不动 AppSettings）。

---

### [P2-13] main_window.py:718-732 — 取消后立即把 UI 复位为空闲，可与残留 worker 并发开新任务（translator + extractor 双侧）

`_on_cancel` 设 `_translating=False`/`_extracting=False` 并立刻重新启用"开始翻译/提取"按钮，但 worker 实际要等当前 LLM 调用（最长 120s）或 whisper 单次 transcribe 结束才退出。此时：
- 用户立刻重新开始翻译 → 新 facade 与旧残留 worker 并发跑（旧 worker 的输出已被 cancel_event 拦住不会落盘，但 API 配额双耗、日志/信号仍可能迟到）；
- extractor 侧更糟：`ExtractionWorker` 的 `extraction_progress`/`extraction_completed` 迟到信号仍连接在主窗口上，取消后继续刷新进度条、`add_files` 往列表塞文件、覆盖状态栏消息——用户已看到"已取消"却又看见进度在动。

根因之一在 facade 契约：`TranslatorFacade._on_worker_all_completed` 在 `_cancelled=True` 时直接 return，**吞掉了 all_completed**（translator.py:555-556），所以 GUI 只能选择"立即复位"，无法等"真正的完成信号"。

修复（跨层）：
1. facade：取消路径也要给 GUI 一个终态——`_cancelled` 时改为发 `self._agg.all_completed.emit(dict(self._results_accum))`（或在 `TranslationSignals` 增加 `cancelled = pyqtSignal()` 并转发）；
2. main_window：`_on_cancel` 不再自行复位，改为等待该终态信号统一走 `_on_all_completed`/新 `_on_cancelled` 槽复位；期间按钮保持禁用；
3. 兜底：迟到信号过滤——`_on_extraction_progress` 等槽入口检查 `if not self._extracting: return`。

---

### [P2-14] core/translator.py:328 + main_window 全局 — 批次结果对象跨线程共享且无拷贝，GUI 编辑与 worker 合并读写同一对象

`batch_completed.emit(filepath, batch_idx, batch_results)` 直接把 worker 线程创建的 `TranslationResult` 列表交给 GUI；main_window 把这些**同一批 Python 对象**存进 `_current_results`，随后 GUI 线程会改写它们（preview `_on_cell_changed`、`EditorDialog._on_save`、`_on_entry_retranslated`）。而 worker 在全部批次结束后执行 `merger.merge(entries, all_results)`（translator.py:365）时**再次读取同一批对象**。GIL 保证不会崩，但存在 GUI 改了一半（text 已改、status 未改）被 worker 读走的不一致窗口，且"确认分析"路径已经用 `copy.deepcopy` 切断共享（translator.py:570），批次路径却没有——同层标准不一致。

修复建议：facade 转发批次结果时做浅拷贝列表 + 逐对象 `copy.copy(r)`（或 dataclasses.replace），成本可忽略（每批几十条）；或在 merger 读取侧改为 worker 自持的独立快照。

---

### [P3-15] analysis_panel.py:255-260 — clear() 不重置 _filepath，current_filepath 返回过期路径

`clear()` 清空树/表/`_analysis` 但保留 `self._filepath`。`MainWindow._on_analysis_approve_clicked`/`_on_analysis_approve_all` 依赖 `analysis_panel.current_filepath` 路由确认。面板隐藏期间点击不可达，但一旦时序交错（如新一轮翻译 clear 后、旧文件迟到的确认点击——按钮 disabled 兜底）路由对象就是旧文件。一行修复：`clear()` 里加 `self._filepath = ""`。

### [P3-16] main_window.py:738-752 — _load_preview 兜底路径在主线程同步解析大字幕文件

后台条目加载失败/未完成时 `SubtitleFile().load(filepath)` 直接在 GUI 线程跑（注释已自知）。大 ASS 文件（数万行）会冻结界面数百毫秒到秒级。建议复用 `_LoadEntriesTask` 模式：`_load_preview` 也走后台，完成后再 `load_entries`。

### [P3-17] main_window.py:708-716、475-489 — 取消/完成后从不 disconnect facade 信号，迟到信号继续污染状态栏

取消后 `log_message`/`progress_updated`/`file_failed` 等仍连接着，worker 退出前的尾部信号会覆盖"已取消"提示、重设进度条。建议在 `_on_all_completed`/取消终态槽里统一 `disconnect`（配合 P1-3 的正确断开 API）。

### [P3-18] corpus_panel.py:122-135 — 清空译法列时旧词条残留

编辑某行译法为空串时：`col==0` 不成立 → 不删旧键；`new_tgt` 为空 → 不写入新值。结果表格显示空，`CorpusManager` 里旧词条仍在，直到下一次 `_populate`（搜索/导入）又"复活"。应在 `new_src and not new_tgt` 时 `self._corpus.remove(new_src)`。

### [P3-19] theme_manager.py:124-128 — `cw.size() or widget.size()` 永远取前者（QSize 无 __bool__）

`QSize` 未定义 `__bool__`，任何 QSize 都为真，`or` 的回退分支是死代码。若本意是空尺寸回退，应写 `cw.size() if not cw.size().isNull() else widget.size()`。

### [P3-20] settings_dialog.py:294-305 — 切换 provider 清空 key_input，已输入未保存的 Key 丢失

用户在 OpenAI 页输入了 Key 但尚未保存，误切到另一 provider 再切回：`self.key_input.clear()` 使输入丢失（只剩"已设置"占位提示）。建议切换前缓存各 provider 的未保存输入，切回时还原。

### [P3-21] settings_dialog.py:699-758 — 模型下载线程不可取消，对话框关闭后仍在后台下载

`_Task` 提交到全局线程池后没有任何取消通道，`SettingsDialog` 被 reject/关闭后下载继续（连接随对话框销毁自动断开，不会崩，但 1.6GB 下载无法中止也无法再看到进度）。建议给 `download_model_with_progress` 传 cancel_event 并在对话框 `finished` 时置位。

### [P3-22] main_window.py:947-976 — 导出用 shutil.copy2 静默覆盖目标目录同名文件

`_on_export` 对 `Path(out_dir)/translated.name` 直接 `copy2`，不检查已存在。建议 `dest.exists()` 时弹问（覆盖/跳过/全部覆盖）。

### [P3-23] main_window.py:510-520 — 重复翻译同一文件时 _on_file_started 清空 _current_results，内存中的人工编辑无提示丢失

重跑同一文件会把 `self._current_results[filepath] = {}`，预览里所有"已修改/已审核"标记与编辑全部清零。磁盘侧有 `_resolve_output_path` 的 `.bak` 备份兜底，但内存编辑（尚未保存到盘的）直接蒸发。建议开跑前检测该文件存在未保存修改（results 中 status==manually_edited）时弹确认。

### [P3-24] main_window.py:749-769（editor_dialog 保存路径） — 对话框编辑不触发自动保存，与重译路径口径不一

`_on_entry_double_clicked` → `EditorDialog._on_save` 改完 result 后只 `_refresh_preview()`，不 `_schedule_auto_save()`；用户必须记得点"保存修改"，或依赖退出时 closeEvent 的 flush。建议在对话框 accept 后同样调用 `_schedule_auto_save()`，统一"编辑即持久化"语义。

### [P3-25] 全局（main_window/settings_dialog 与 worker 的设置读取） — 翻译进行中修改设置会实时影响进行中的任务

`AppSettings` 是单例 + 每次属性访问实时读 QSettings；worker 每批循环读 `settings.max_qc_retries`/`chunk_tokens`（translator.py:253、335），每个新文件读 `provider`/`model`（:422-426）。翻译中途在设置对话框改 provider/model/质检参数会即时作用于后续批次——无锁、无提示。`QSettings` 本身可重入不会崩，但行为不可预期。建议 `_on_translate` 启动时快照一份只读配置传给 worker（构造 TranslatorWorker 时固化），或翻译期间在设置对话框顶部显示"翻译进行中，更改将在下一轮生效"。

---

## 二、专项核对结论（用户指定关注点中未发现问题的项）

- 文件对话框取消处理：`main_window._on_export`（getExistingDirectory 空串即 return）、`file_panel._on_add_files`（空列表不 add）、`settings_dialog._pick_bg_image`（空 path 跳过）均正确处理取消，无缺陷。
- `progress_widget.py`、`editor_dialog.py`、`file_panel.py`：未发现线程/崩溃级问题，仅 P3 级别小项（见 #22/#24）。
- `corpus_panel.py`：信号连接（`cellChanged`、按钮）均在主线程，`blockSignals` 使用正确，无跨线程问题。
- `theme_manager.py`：单例初始化与 darkdetect 调用均在主线程，无阻塞风险。
- 信号连接类型：worker→GUI 全部为默认 AutoConnection（跨线程自动 queued），`_WorkerDone`/`_Sig` 模式的跨线程投递方向正确；未发现误用 DirectConnection。

---

## 三、严重度汇总统计

| 严重度 | 数量 | 编号 |
|--------|------|------|
| P0（崩溃/丢数据） | 2 | #1, #2 |
| P1（功能错误） | 6 | #3, #4, #5, #6, #7, #8 |
| P2（健壮性缺陷） | 6 | #9, #10, #11, #12, #13, #14 |
| P3（代码质量） | 11 | #15 – #25 |
| **合计** | **25** | |

| 关注维度 | 对应问题 |
|----------|----------|
| Qt 线程规则 | #10, #14 |
| 长任务阻塞主线程 | #7, #8, #16 |
| 取消/关闭流程 | #2, #4, #8, #13 |
| 槽内异常 | #1 |
| 状态一致性/竞态 | #3, #5, #6, #9, #11, #12, #13, #23, #25 |
| 资源泄漏/信号未断开 | #3, #17, #21 |

## 四、Top 5 一句话摘要

1. **[P0] preview_panel.py:375**：右键预览表格引用未定义变量 `idx_item` → NameError → PyQt5 qFatal abort，应用必崩且右键功能全废。
2. **[P0] main_window.py:888**：closeEvent 不调用已实现的 `translator.cancel()`/`extractor.cancel()`，翻译中关窗 → 退出阶段 QThread 析构崩溃或 `_zh` 输出文件写一半丢数据。
3. **[P1] main_window.py:329/423**：`conn.disconnect()` 是无效调用（实测 `QMetaObject.Connection` 无此方法，异常被吞），"断开旧信号"从未生效。
4. **[P1] main_window.py:370 + 336-352**：安全超时只复位 UI 不取消 worker（后台继续写文件），且 `approval_timed_out` 信号 GUI 侧从未连接——确认超时后分析面板永久卡在等待态、确认按钮点击被静默忽略。
5. **[P1] main_window.py:800-869**：单条重译回调按回调时刻的 `_current_file` 归属结果，并行切换预览文件期间完成的重译会写进另一个文件并随自动保存落盘。
