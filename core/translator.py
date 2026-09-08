"""翻译编排器 — QThread Worker 模式，三阶段流水线"""

import copy
import logging
import threading
from pathlib import Path

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from config.settings import AppSettings
from core.subtitle_io import SubtitleFile
from core.analyzer import SubtitleAnalyzer
from core.chunker import SubtitleChunker
from core.qc_checker import QCChecker
from core.merger import Merger
from core.tag_handler import ASSTagHandler
from core.translation_batch import (
    SubtitleEntry,
    TranslationBatch,
    TranslationResult,
    AnalysisResult,
)
from services.openai_service import OpenAIService
from services.claude_service import ClaudeService
from services.base_llm import BaseLLMService

log = logging.getLogger("subtitle_translator")

# 模块级运行中线程注册表：持有 (QThread, worker) 的强引用，直到 finished 后再移除。
# 关键作用：facade 被替换/GC（如安全计时器超时恢复 UI 后重新开始翻译）时，
# 仍在运行的 QThread 由注册表兜底持有——否则 C++ 侧 QThread 在子线程仍运行时
# 被析构，会触发 "QThread: Destroyed while thread is still running" 硬崩溃。
_live_threads: dict[int, tuple[QThread, "TranslatorWorker"]] = {}


def _cleanup_finished_thread(thread: QThread, worker: "TranslatorWorker") -> None:
    """线程结束后回收（由 finished 信号触发，独立于 facade 存活与否）。

    从模块级注册表移除并 deleteLater 释放。此后引用可安全释放
    （线程已停止运行），不会触发 QThread 强杀崩溃。
    """
    if _live_threads.pop(id(thread), None) is None:
        return
    try:
        worker.deleteLater()
    except RuntimeError:
        pass
    try:
        thread.deleteLater()
    except RuntimeError:
        pass


class TranslationSignals(QObject):
    """翻译信号发射器"""

    file_started = pyqtSignal(str)                      # filepath
    file_analysis_done = pyqtSignal(str, object)         # filepath, AnalysisResult
    waiting_for_approval = pyqtSignal(str)               # filepath — 等待用户确认分析
    analysis_approved = pyqtSignal(str)                  # filepath — 用户已确认，继续翻译
    approval_timed_out = pyqtSignal(str)                 # filepath — 确认等待超时，继续翻译
    batch_started = pyqtSignal(str, int, int)            # filepath, batch_id, total
    batch_completed = pyqtSignal(str, int, object)       # filepath, batch_id, results
    batch_qc_failed = pyqtSignal(str, int, object)       # filepath, batch_id, results
    batch_retry = pyqtSignal(str, int, int)              # filepath, batch_id, retry_num
    file_completed = pyqtSignal(str, str)                # filepath, output_path
    file_failed = pyqtSignal(str, str)                   # filepath, error_message
    progress_updated = pyqtSignal(str, int, int, str)    # filepath, current, total, status
    all_completed = pyqtSignal(dict)                     # {filepath: output_path}
    log_message = pyqtSignal(str)                        # log text


class TranslatorWorker(QObject):
    """翻译工作线程"""

    def __init__(self, filepaths: list[str]) -> None:
        super().__init__()
        self.filepaths = filepaths
        self.signals = TranslationSignals()
        self._cancel_event = threading.Event()
        self._continue_event = threading.Event()
        self._edited_analysis: AnalysisResult | None = None
        self._tag_handler = ASSTagHandler()
        # 当前正在等待确认的文件（供 Facade 按文件路由 approve）
        self.waiting_file: str | None = None

    def approve_analysis(self, edited_analysis: AnalysisResult) -> None:
        """主线程调用：确认分析结果，继续翻译"""
        self._edited_analysis = edited_analysis
        self._continue_event.set()

    def run(self) -> None:
        """在工作线程中执行"""
        try:
            settings = AppSettings()
            source_lang = settings.source_lang
            target_lang = settings.target_lang

            results: dict[str, str] = {}

            for filepath in self.filepaths:
                if self._cancel_event.is_set():
                    break

                try:
                    output = self._translate_one_file(filepath, source_lang, target_lang)
                    results[filepath] = output
                except Exception as e:
                    log.exception("文件翻译失败: %s", filepath)
                    results[filepath] = ""  # 空串标记失败，让 all_completed 统计正确
                    self.signals.file_failed.emit(filepath, str(e))

            self.signals.all_completed.emit(results)
        finally:
            # 关键：run() 作为 started 的槽执行，返回后 QThread 会进入 exec() 事件循环
            # 永远阻塞。必须在结束前 quit，否则线程永不退出（泄漏 + 退出时崩溃）。
            from PyQt5.QtCore import QThread
            QThread.currentThread().quit()

    def _translate_one_file(
        self, filepath: str, source_lang: str, target_lang: str
    ) -> str:
        """翻译单个文件的三阶段流水线"""
        settings = AppSettings()
        self.signals.file_started.emit(filepath)
        self.signals.log_message.emit(f"开始处理: {Path(filepath).name}")

        # 加载字幕
        sub_file = SubtitleFile()
        entries = sub_file.load(filepath)

        # 源语言=自动检测：按文件内容判定（每个文件独立）
        if source_lang == "auto":
            from core.analyzer import detect_source_lang, LANG_NAMES
            source_lang = detect_source_lang(entries)
            self.signals.log_message.emit(
                f"自动检测源语言: {LANG_NAMES.get(source_lang, source_lang)}"
            )

        # 标签提取（所有条目的 ASS/SRT 标签 → 占位符）；还原由 merger 在
        # 合并阶段统一完成（从 original_text 重新提取映射，无需在此保留）
        for entry in entries:
            cleaned, _ = self._tag_handler.extract_tags(entry.original_text)
            entry.text = cleaned

        # 判定应跳过的条目（ASS 绘图块/纯音效/空行）——不翻译、不质检，
        # 最终合并时透传原文（含标签）
        skip_indices = {
            entry.index for entry in entries
            if self._tag_handler.is_skip_entry(entry.original_text)
        }
        translatable = [e for e in entries if e.index not in skip_indices]
        if not translatable:
            self.signals.log_message.emit("字幕不含可翻译的文本条目")
            return self._save_pass_through(filepath, entries)

        if self._cancel_event.is_set():
            return ""

        # ---- 阶段一: 全篇分析 ----
        self.signals.log_message.emit("阶段一: 全篇分析...")
        self.signals.progress_updated.emit(filepath, 0, 100, "分析中")

        analyzer = SubtitleAnalyzer()
        llm = self._get_llm_service()

        # 只发送可翻译条目：绘图/音效跳过项的 ASS 向量文本动辄数 KB，
        # 混进分析预览既浪费上下文又干扰话题判断。条目保留原始 index，
        # LLM 返回的 start_idx 与 chunker 的索引映射仍然对齐
        analysis_prompt = analyzer.build_analysis_prompt(
            translatable, source_lang, target_lang
        )
        analysis = None
        last_err: Exception | None = None
        # 分析失败只损失上下文质量、不丢数据，重试一次即可（翻译路径有
        # _execute 的 3 次重试，分析路径此前一次抖动就直接放弃）
        for attempt in (1, 2):
            try:
                response = llm.analyze(analysis_prompt)
                analysis = analyzer.parse_analysis_response(response)
                last_err = None
                break
            except Exception as e:
                last_err = e
                analysis = None
                if attempt == 1:
                    log.warning("全篇分析失败，重试一次: %s", e)
        if last_err is not None:
            log.warning("全篇分析失败，将跳过分析直接翻译: %s", last_err)
            self.signals.log_message.emit(f"全篇分析失败，跳过: {last_err}")

        if analysis is not None:
            self.signals.file_analysis_done.emit(filepath, analysis)
            self.signals.log_message.emit(
                f"分析完成: {len(analysis.topic_segments)} 个话题, "
                f"{len(analysis.glossary)} 个术语"
            )

        if self._cancel_event.is_set():
            return ""

        # 等待用户确认/修改分析结果，每秒检查是否取消
        if analysis is not None:
            self._continue_event.clear()
            self.waiting_file = filepath
            self.signals.waiting_for_approval.emit(filepath)
            self.signals.log_message.emit("等待确认分析结果，请在左侧面板修改并点击「确认继续翻译」")
            approved = False
            for _ in range(300):  # 最多等 300 秒
                if self._cancel_event.is_set():
                    self.signals.log_message.emit("分析已取消")
                    break
                if self._continue_event.wait(timeout=1):
                    approved = True
                    break
            self.waiting_file = None
            if self._cancel_event.is_set():
                return ""
            if not approved:
                self.signals.log_message.emit("使用原始分析结果继续翻译（确认超时）")
                # 通知 facade 推进确认队列：否则 _showing_approval 永远停留
                # 在本文件上，后续文件的确认面板永远不展示（多文件批量时
                # 每个文件静默白等 300 秒且 UI 无提示）
                self.signals.approval_timed_out.emit(filepath)
            elif self._edited_analysis is not None:
                analysis = self._edited_analysis
                self.signals.analysis_approved.emit(filepath)
                self.signals.log_message.emit("已使用修改后的分析结果")
            self._edited_analysis = None  # 重置，避免跨文件泄漏

        if self._cancel_event.is_set():
            return ""

        # ---- 阶段二: 分批翻译 ----
        chunker = SubtitleChunker()
        batches = chunker.chunk(translatable, analysis, source_lang)

        # QC 术语一致性检查用的强制术语表：语料库（最高优先级）覆盖分析术语
        # （用户确认过的分析 glossary 视同认可译法）。之前 check() 从不传
        # glossary，术语检查是死代码——强制术语翻错也不会被检出
        from core.corpus_manager import CorpusManager
        forced_glossary: dict[str, str] = {}
        if analysis is not None and analysis.glossary:
            forced_glossary.update(analysis.glossary)
        forced_glossary.update(CorpusManager().get_all())

        self.signals.log_message.emit(f"阶段二: 分批翻译 ({len(batches)} 批)")
        self.signals.progress_updated.emit(filepath, 0, len(batches), "翻译中")

        all_results: dict[int, TranslationResult] = {}
        qc_checker = QCChecker()

        for batch_idx, batch in enumerate(batches):
            if self._cancel_event.is_set():
                break

            self.signals.batch_started.emit(filepath, batch_idx, len(batches))
            self.signals.progress_updated.emit(
                filepath, batch_idx + 1, len(batches), f"批次 {batch_idx + 1}/{len(batches)}"
            )

            # 翻译
            retry_count = 0
            # 取消检查放进循环条件：否则取消后当前批次仍会跑完全部
            # 服务层×QC 层重试（最坏 9 次 API 调用、15~20 分钟）
            while retry_count <= settings.max_qc_retries and not self._cancel_event.is_set():
                try:
                    translated = llm.translate_batch(batch, source_lang, target_lang)
                except Exception as e:
                    if self._cancel_event.is_set():
                        break  # 取消立即跳出，不再重试
                    log.error("批次 %d 翻译失败: %s", batch_idx, e)
                    if retry_count < settings.max_qc_retries:
                        retry_count += 1
                        self.signals.batch_retry.emit(filepath, batch_idx, retry_count)
                        continue
                    # 重试耗尽：回退原文，但显式标记为需审核并上报，
                    # 避免「原文被静默当成功译文存盘」而无用户感知
                    primary_entries = batch.entries[batch.primary_start_idx : batch.primary_end_idx]
                    fallback_results = [
                        TranslationResult(
                            index=entry.index,
                            original_text=entry.text,
                            translated_text=entry.text,
                            status="needs_review",
                            qc_issues=["翻译调用失败，已回退原文，请人工审核"],
                        )
                        for entry in primary_entries
                    ]
                    for r in fallback_results:
                        all_results[r.index] = r
                    self.signals.batch_qc_failed.emit(filepath, batch_idx, fallback_results)
                    self.signals.log_message.emit(
                        f"批次 {batch_idx} 翻译失败，已回退原文并标记为需审核"
                    )
                    break

                # ---- 阶段三: 质检 ----
                primary_entries = batch.entries[batch.primary_start_idx : batch.primary_end_idx]
                filled_from_original: set[int] = set()
                if len(translated) != len(primary_entries):
                    # 长度不匹配，尝试修复（缺失部分用原文回填）
                    if len(translated) < len(primary_entries):
                        fill_start = len(translated)
                        translated += [
                            primary_entries[i].text
                            for i in range(fill_start, len(primary_entries))
                        ]
                        filled_from_original.update(
                            primary_entries[i].index
                            for i in range(fill_start, len(primary_entries))
                        )
                    else:
                        translated = translated[: len(primary_entries)]

                batch_results = qc_checker.check(
                    primary_entries, translated, forced_glossary
                )

                # 回填原文的条目并未被真正翻译：标记为需审核，
                # 防止短行/符号类条目被 QC 启发式放行为「成功译文」静默存盘
                for r in batch_results:
                    if r.index in filled_from_original:
                        r.status = "needs_review"
                        if "原文回填" not in r.qc_issues:
                            r.qc_issues.append("长度不匹配，原文回填，请人工审核")

                if qc_checker.is_batch_passable(batch_results):
                    # 保留每条的实际质检状态，不强制覆盖。
                    # 放行批次里的 qc_failed 条目降级为 needs_review 并标注——
                    # 否则「译文=原文」这类未翻译行会以成功形态静默落盘，
                    # 成品无任何需审核痕迹
                    for r in batch_results:
                        if r.status == "qc_failed":
                            r.status = "needs_review"
                            if "批次放行，但本条存在未解决质检问题" not in r.qc_issues:
                                r.qc_issues.append("批次放行，但本条存在未解决质检问题")
                        elif r.status != "needs_review":
                            r.status = "qc_passed"
                        all_results[r.index] = r
                    self.signals.batch_completed.emit(filepath, batch_idx, batch_results)
                    break
                else:
                    issues = [
                        i for r in batch_results for i in r.qc_issues
                    ]
                    retry_count += 1
                    if retry_count <= settings.max_qc_retries:
                        self.signals.batch_retry.emit(filepath, batch_idx, retry_count)
                        self.signals.log_message.emit(
                            f"批次 {batch_idx} 质检未通过，重试 {retry_count}"
                        )
                        # 为下一轮构建更严格的 prompt（回灌文本为 LLM 产物，
                        # 截断并加引号块包裹，降低提示词注入与格式污染风险）
                        issues_text = "\n".join(issues[:5])[:500]
                        batch.topic_context += (
                            f'\n\n特别提醒: 上次翻译有以下问题，请修正:\n"""{issues_text}"""'
                        )
                    else:
                        # 标记为需审核
                        for r in batch_results:
                            r.status = "needs_review"
                            all_results[r.index] = r
                        self.signals.batch_qc_failed.emit(filepath, batch_idx, batch_results)
                        self.signals.log_message.emit(
                            f"批次 {batch_idx} 质检多次失败，标记为需审核"
                        )
                        break

        if self._cancel_event.is_set():
            return ""

        # ---- 合并并保存 ----
        self.signals.log_message.emit("合并翻译结果...")
        merger = Merger()
        # 标签还原由 merger 统一完成（此前 translator 先还原一遍、merger 内部
        # 再做一遍，双重正则提取纯属浪费；merger 对无占位符文本是 no-op）
        merged = merger.merge(entries, all_results)

        output_path = self._resolve_output_path(filepath)
        sub_file.save(output_path, merged, filepath)

        # 取消后不再上报完成，避免 UI 显示「已完成」
        if self._cancel_event.is_set():
            return ""

        self.signals.file_completed.emit(filepath, output_path)
        self.signals.log_message.emit(f"完成: {Path(filepath).name} → {Path(output_path).name}")
        return output_path

    def _resolve_output_path(self, filepath: str) -> str:
        """计算输出路径；同名 _zh 文件已存在时先备份旧文件，避免静默覆盖。

        用户手工编辑过的 {stem}_zh{ext} 会被重新翻译直接覆盖而丢数据，
        这里先把它改名为 .bak 后缀保留，再写入新的翻译结果。
        """
        out = Path(filepath).parent / f"{Path(filepath).stem}_zh{Path(filepath).suffix}"
        if out.exists():
            n = 0
            backup = out.with_name(f"{out.stem}.bak{out.suffix}")
            while backup.exists():
                n += 1
                backup = out.with_name(f"{out.stem}.bak{n}{out.suffix}")
            try:
                out.replace(backup)
                log.warning("输出文件 %s 已存在，旧文件已备份为: %s", out.name, backup.name)
                self.signals.log_message.emit(
                    f"同名输出 {out.name} 已存在，旧文件已备份为 {backup.name}"
                )
            except OSError as e:
                # 备份失败 → 中止本次保存：直接覆盖会让用户唯一的好翻译
                # 毫无副本地被坏文件顶掉（上层 per-file try 会捕获并上报失败）
                log.error("备份旧输出文件 %s 失败: %s，已取消写入", out, e)
                self.signals.log_message.emit(
                    f"错误: 无法备份旧输出 {out.name}（{e}），本次跳过保存"
                )
                raise OSError(f"输出文件备份失败，已取消写入 {out.name}: {e}") from e
        return str(out)

    def _save_pass_through(self, filepath: str, entries: list[SubtitleEntry]) -> str:
        """全部条目都是跳过项（绘图/音效）时，还原标签并原样保存"""
        for entry in entries:
            _, tag_map = self._tag_handler.extract_tags(entry.original_text)
            entry.text = self._tag_handler.restore_tags(entry.text, tag_map)
        output_path = self._resolve_output_path(filepath)
        sub_file = SubtitleFile()
        sub_file.save(output_path, entries, filepath)
        if not self._cancel_event.is_set():
            self.signals.file_completed.emit(filepath, output_path)
            self.signals.log_message.emit(
                f"完成（无文本可译）: {Path(filepath).name} → {Path(output_path).name}"
            )
        return output_path

    def _get_llm_service(self) -> BaseLLMService:
        settings = AppSettings()
        if settings.provider == "anthropic":
            return ClaudeService()
        return OpenAIService()

    def cancel(self) -> None:
        self._cancel_event.set()


class TranslatorFacade(QObject):
    """翻译编排器（主线程端）——支持多 worker 并行处理多个文件

    负责：创建多个 QThread+Worker、把各 worker 的信号聚合转发给 UI、
    按文件路由分析确认、合并 all_completed 结果、统一取消与清理。
    """

    def __init__(self) -> None:
        super().__init__()
        self._settings = AppSettings()
        self._threads: list[QThread] = []
        self._workers: list[TranslatorWorker] = []
        self._agg: TranslationSignals = TranslationSignals()
        self._pending_analysis: dict[str, AnalysisResult] = {}
        self._results_accum: dict[str, str] = {}
        self._completed_workers = 0
        # 分析确认 UI 串行化：同一时刻只向面板展示一个待确认文件，其余排队
        self._approval_queue: list[str] = []
        self._showing_approval: str | None = None
        # 取消标记：取消后不再向 UI 发 all_completed（避免迟到信号误触发）
        self._cancelled = False

    def prepare_translation(self, filepaths: list[str]) -> None:
        """创建 N 个 worker（N = parallel_files），把文件均分，但不启动线程"""
        if self._threads:
            log.warning("翻译已在进行中，强制清理旧线程")
            self._force_cleanup()

        self._pending_analysis.clear()
        self._results_accum.clear()
        self._completed_workers = 0
        self._approval_queue.clear()
        self._showing_approval = None
        self._cancelled = False
        self._agg = TranslationSignals()

        n = max(1, min(self._settings.parallel_files, len(filepaths)))
        chunks = [filepaths[i::n] for i in range(n)]
        chunks = [c for c in chunks if c]

        for chunk in chunks:
            worker = TranslatorWorker(chunk)
            thread = QThread()
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            # 不 connect deleteLater：线程对象生命周期由 facade 拥有，
            # 清理统一走 _force_cleanup（finished 后 quit/wait 均安全）

            # 直接转发（无需额外处理的信号）
            worker.signals.file_started.connect(self._agg.file_started)
            worker.signals.batch_started.connect(self._agg.batch_started)
            worker.signals.batch_completed.connect(self._agg.batch_completed)
            worker.signals.batch_qc_failed.connect(self._agg.batch_qc_failed)
            worker.signals.batch_retry.connect(self._agg.batch_retry)
            worker.signals.file_completed.connect(self._agg.file_completed)
            worker.signals.file_failed.connect(self._agg.file_failed)
            worker.signals.progress_updated.connect(self._agg.progress_updated)
            worker.signals.log_message.connect(self._agg.log_message)
            worker.signals.analysis_approved.connect(self._agg.analysis_approved)

            # 需要聚合/路由的信号
            worker.signals.file_analysis_done.connect(self._on_worker_analysis_done)
            worker.signals.waiting_for_approval.connect(self._on_worker_waiting)
            worker.signals.approval_timed_out.connect(self._on_worker_approval_timed_out)
            worker.signals.all_completed.connect(self._on_worker_all_completed)

            self._workers.append(worker)
            self._threads.append(thread)
            # 登记到模块级注册表：持有线程/worker 的强引用，线程仍在运行时
            # 引用绝不提前释放（见 _live_threads 注释）。
            # finished 连接用模块级函数（lambda 捕获线程/worker），
            # 不依赖 facade 存活——facade 被 GC 后线程结束仍能正确回收。
            _live_threads[id(thread)] = (thread, worker)
            thread.finished.connect(
                lambda t=thread, w=worker: _cleanup_finished_thread(t, w)
            )

    def start(self) -> None:
        """启动所有 worker 线程"""
        for thread in self._threads:
            thread.start()

    def start_translation(self, filepaths: list[str]) -> None:
        """便捷方法：准备并直接启动"""
        self.prepare_translation(filepaths)
        self.start()

    # ---- 信号聚合 ----

    def _on_worker_analysis_done(self, filepath: str, analysis: AnalysisResult) -> None:
        # 只缓存，不立即转发——等该文件进入确认队列再展示，避免后到分析
        # 覆盖用户正在审阅的面板（并行多文件时）
        self._pending_analysis[filepath] = analysis

    def _on_worker_waiting(self, filepath: str) -> None:
        if self._showing_approval is None:
            self._show_approval(filepath)
        elif filepath not in self._approval_queue:
            self._approval_queue.append(filepath)

    def _on_worker_approval_timed_out(self, filepath: str) -> None:
        """某文件的确认等待超时：清理展示状态并推进队列，
        否则 _showing_approval 永远停留、后续文件面板永不展示"""
        self._pending_analysis.pop(filepath, None)
        if self._showing_approval == filepath:
            self._showing_approval = None
            while self._approval_queue:
                nxt = self._approval_queue.pop(0)
                if any(w.waiting_file == nxt for w in self._workers):
                    self._show_approval(nxt)
                    break

    def _show_approval(self, filepath: str) -> None:
        """把某个文件的待确认分析展示到面板"""
        self._showing_approval = filepath
        analysis = self._pending_analysis.get(filepath)
        if analysis is not None:
            self._agg.file_analysis_done.emit(filepath, analysis)
        self._agg.waiting_for_approval.emit(filepath)

    def _on_worker_all_completed(self, results: dict[str, str]) -> None:
        self._results_accum.update(results)
        self._completed_workers += 1
        if self._cancelled:
            return  # 已取消：不再向 UI 报最终结果
        # 全部 worker 结束后才向 UI 报告一次最终结果
        if self._completed_workers >= len(self._workers):
            self._agg.all_completed.emit(dict(self._results_accum))

    def approve_analysis(self, filepath: str, analysis: AnalysisResult | None) -> None:
        """确认「面板当前展示」的文件：路由到对应 worker，然后展示下一个待确认的"""
        if self._showing_approval != filepath:
            return  # 面板当前展示的不是这个文件（忽略迟到的确认）
        approved = False
        for worker in self._workers:
            if worker.waiting_file == filepath:
                # 深拷贝切断与 GUI 面板的共享：确认后面板仍可编辑，
                # worker 线程遍历同一对象会遇到并发修改
                worker.approve_analysis(copy.deepcopy(analysis))
                approved = True
                break
        self._pending_analysis.pop(filepath, None)
        self._showing_approval = None
        # 展示下一个排队等待确认的文件（跳过已超时/已不再等待的）
        while self._approval_queue:
            nxt = self._approval_queue.pop(0)
            if any(w.waiting_file == nxt for w in self._workers):
                self._show_approval(nxt)
                break
        if not approved:
            log.warning("approve_analysis 未找到等待文件 %s 的 worker", filepath)

    def approve_all(
        self,
        current_filepath: str | None = None,
        current_analysis: AnalysisResult | None = None,
    ) -> None:
        """批量确认所有正在等待的文件（批量处理场景）。

        - 当前面板展示的文件用修改后的分析确认
        - 其余等待文件用各自的分析结果（None）确认
        """
        if current_filepath and current_analysis is not None:
            for worker in self._workers:
                if worker.waiting_file == current_filepath:
                    worker.approve_analysis(copy.deepcopy(current_analysis))
                    break
        for worker in self._workers:
            if worker.waiting_file and worker.waiting_file != current_filepath:
                worker.approve_analysis(None)
        self._approval_queue.clear()
        self._showing_approval = None

    def cancel(self) -> None:
        """取消所有 worker 并清理"""
        self._cancelled = True
        for worker in self._workers:
            try:
                worker.cancel()
            except RuntimeError:
                pass  # 线程对象已被销毁（防御）
        self._force_cleanup()

    def cancel_no_wait(self) -> None:
        """取消但不阻塞等待：供 UI 取消按钮/安全超时使用。

        wait(3000)×N 在主线程串行执行会让 UI 冻结最长 N×3 秒。未退出线程
        由模块级 _live_threads 注册表兜底持有，引用不会提前释放；worker
        已全部收到取消事件，会在当前调用返回后尽快退出。
        进程退出（closeEvent）必须用 cancel()——解释器拆卸会析构仍在
        运行的 QThread 触发硬崩溃，那里需要真正的等待。
        """
        self._cancelled = True
        for worker in self._workers:
            try:
                worker.cancel()
            except RuntimeError:
                pass
        self._force_cleanup(wait=False)

    def _force_cleanup(self, *, wait: bool = True) -> None:
        """强制清理线程引用，但线程未完全退出时绝不释放引用。

        Qt 规定：QThread 在子线程仍运行时被析构会触发
        "QThread: Destroyed while thread is still running" 硬崩溃
        （worker 阻塞在最长 120s 的 LLM API 调用中时常见，取消/超时命中概率高）。
        因此这里只做三件事：
        1) 通知所有 worker 取消（cancel 事件，LLM 调用返回后 run() 会尽快退出）；
        2) quit()+wait() 等待线程自然结束（run() 的 finally 会 quit 事件循环）；
           wait=False 跳过等待（UI 取消路径防冻结），线程交给注册表兜底；
        3) 未退出的线程仍被模块级 _live_threads 注册表持有（直到 finished 信号
           触发 _cleanup_finished_thread 才回收）——期间引用绝不提前 clear()。
        """
        for worker in self._workers:
            try:
                worker.cancel()
            except RuntimeError:
                pass  # 线程对象已被销毁（防御）
        for thread in self._threads:
            try:
                thread.requestInterruption()
                thread.quit()
            except RuntimeError:
                pass
        if wait:
            for thread in self._threads:
                try:
                    if thread.isRunning():
                        thread.wait(3000)
                except RuntimeError:
                    pass
        self._threads.clear()
        self._workers.clear()

    @property
    def signals(self) -> TranslationSignals:
        return self._agg
