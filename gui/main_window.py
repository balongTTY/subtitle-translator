"""主窗口 — QMainWindow 组装所有面板"""

import logging
import shutil
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QAction, QMessageBox, QStatusBar,
    QFileDialog, QPushButton,
)

from config.settings import AppSettings
from gui.theme_manager import ThemeManager
from gui.file_panel import FilePanel
from gui.preview_panel import PreviewPanel
from gui.analysis_panel import AnalysisPanel
from gui.progress_widget import ProgressWidget
from gui.settings_dialog import SettingsDialog
from gui.editor_dialog import EditorDialog
from core.translator import TranslatorFacade
from core.extractor import ExtractionFacade, check_whisper_available
from core.analyzer import SubtitleAnalyzer
from core.subtitle_io import SubtitleFile
from core.translation_batch import (
    TranslationBatch,
    TranslationResult,
    SubtitleEntry,
    AnalysisResult,
)
from utils.constants import APP_NAME, APP_VERSION, SUPPORTED_FORMATS, MEDIA_FORMATS

log = logging.getLogger("subtitle_translator")


class _WorkerDone(QObject):
    """QThreadPool 任务完成 → 主线程槽 的跨线程回调信号。

    注意：QTimer.singleShot(0, cb) 从工作线程调用时回调不会触发
    （工作线程没有事件循环），必须用 pyqtSignal 的 queued 连接投递。
    """
    done = pyqtSignal(object)


class MainWindow(QMainWindow):
    """主窗口"""

    def __init__(self) -> None:
        super().__init__()
        self.settings = AppSettings()
        self.theme = ThemeManager()
        self.translator = TranslatorFacade()
        self._current_file: str = ""
        # 并行翻译下按文件分组保存预览状态，避免多文件结果互相覆盖
        self._current_entries: dict[str, list[SubtitleEntry]] = {}
        self._current_results: dict[str, dict[int, TranslationResult]] = {}
        # 并行模式下已进入预览的文件（供文件切换下拉）
        self._active_files: list[str] = []
        self._translator_connections: list = []
        self.extractor = ExtractionFacade()
        self._extract_connections: list = []
        self._translating = False
        self._extracting = False
        self._setup_window()
        self._setup_menu()
        self._setup_ui()
        self._connect_signals()
        self._apply_theme()

    def _setup_window(self) -> None:
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.resize(1200, 750)
        self.setMinimumSize(900, 550)

    def _setup_menu(self) -> None:
        menubar = self.menuBar()

        # 文件菜单
        file_menu = menubar.addMenu("文件(&F)")
        add_action = QAction("添加文件...", self)
        add_action.setShortcut(QKeySequence("Ctrl+O"))
        add_action.triggered.connect(lambda: self.file_panel._on_add_files())
        file_menu.addAction(add_action)

        clear_action = QAction("清空列表", self)
        clear_action.triggered.connect(lambda: self.file_panel._on_clear())
        file_menu.addAction(clear_action)

        file_menu.addSeparator()

        export_action = QAction("导出所选...", self)
        export_action.setShortcut(QKeySequence("Ctrl+S"))
        export_action.triggered.connect(self._on_export)
        file_menu.addAction(export_action)

        file_menu.addSeparator()

        exit_action = QAction("退出", self)
        exit_action.setShortcut(QKeySequence("Alt+F4"))
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # 设置菜单
        settings_menu = menubar.addMenu("设置(&S)")
        settings_action = QAction("设置...", self)
        settings_action.triggered.connect(self._on_settings)
        settings_menu.addAction(settings_action)

        theme_menu = settings_menu.addMenu("主题")
        light_action = QAction("浅色", self)
        light_action.triggered.connect(lambda: self.theme.apply_theme("light"))
        dark_action = QAction("深色", self)
        dark_action.triggered.connect(lambda: self.theme.apply_theme("dark"))
        theme_menu.addAction(light_action)
        theme_menu.addAction(dark_action)

        # 帮助菜单
        help_menu = menubar.addMenu("帮助(&H)")
        about_action = QAction("关于", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        # 工具栏：主操作（翻译/提取） | 次操作（导出/保存）
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.translate_btn = QPushButton("▶ 开始翻译")
        self.translate_btn.setObjectName("primaryBtn")
        self.translate_btn.setMinimumHeight(40)
        self.translate_btn.setToolTip("翻译勾选的字幕文件：全篇分析 → 分批翻译 → 质检")
        self.translate_btn.clicked.connect(self._on_translate)
        self.translate_btn.setEnabled(False)
        toolbar.addWidget(self.translate_btn)

        self.extract_btn = QPushButton("✎ 提取字幕")
        self.extract_btn.setToolTip(
            "用 whisper 语音识别从勾选的视频/音频提取字幕（日语/英语优化），\n"
            "生成同名 .srt 后自动加入列表，可直接开始翻译"
        )
        self.extract_btn.setMinimumHeight(40)
        self.extract_btn.clicked.connect(self._on_extract)
        self.extract_btn.setEnabled(False)
        toolbar.addWidget(self.extract_btn)

        sep1 = QWidget()
        sep1.setFixedWidth(1)
        sep1.setMinimumHeight(24)
        sep1.setObjectName("toolbarSep")
        toolbar.addSpacing(6)
        toolbar.addWidget(sep1)
        toolbar.addSpacing(6)

        self.export_btn = QPushButton("导出所选")
        self.export_btn.setToolTip("把勾选文件的 _zh 翻译结果复制到指定目录")
        self.export_btn.setMinimumHeight(40)
        self.export_btn.clicked.connect(self._on_export)
        self.export_btn.setEnabled(False)
        toolbar.addWidget(self.export_btn)

        self.save_edits_btn = QPushButton("保存修改")
        self.save_edits_btn.setToolTip("把预览中编辑/重译的内容写回 _zh 翻译文件")
        self.save_edits_btn.setMinimumHeight(40)
        self.save_edits_btn.clicked.connect(self._on_save_edits)
        self.save_edits_btn.setEnabled(False)
        toolbar.addWidget(self.save_edits_btn)

        toolbar.addStretch()
        main_layout.addLayout(toolbar)

        # 主分割器：左侧文件列表 + 分析面板 | 右侧预览面板
        splitter = QSplitter(Qt.Horizontal)

        # 左侧面板
        left_panel = QSplitter(Qt.Vertical)

        self.file_panel = FilePanel()
        left_panel.addWidget(self.file_panel)

        self.analysis_panel = AnalysisPanel()
        left_panel.addWidget(self.analysis_panel)

        left_panel.setStretchFactor(0, 3)
        left_panel.setStretchFactor(1, 2)
        left_panel.setSizes([420, 260])
        left_panel.setCollapsible(0, False)
        left_panel.setCollapsible(1, False)

        # 右侧：预览 + 术语库 标签页
        from PyQt5.QtWidgets import QTabWidget
        right_tabs = QTabWidget()
        self.preview_panel = PreviewPanel()
        self.preview_panel.entry_double_clicked.connect(self._on_entry_double_clicked)
        right_tabs.addTab(self.preview_panel, "对照预览")

        from gui.corpus_panel import CorpusPanel
        self.corpus_panel = CorpusPanel()
        self.corpus_panel.corpus_changed.connect(self._on_corpus_changed)
        right_tabs.addTab(self.corpus_panel, "术语库")

        splitter.addWidget(left_panel)
        splitter.addWidget(right_tabs)
        splitter.setStretchFactor(0, 35)
        splitter.setStretchFactor(1, 65)
        # 初始比例（stretch 只在整体拉伸时生效，初始尺寸需显式给）
        splitter.setSizes([430, 770])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        self.file_panel.setMinimumWidth(280)
        right_tabs.setMinimumWidth(420)

        main_layout.addWidget(splitter, 1)

        # 底部进度条
        self.progress_widget = ProgressWidget()
        self.progress_widget.cancel_btn.clicked.connect(self._on_cancel)
        main_layout.addWidget(self.progress_widget)

        # 状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪 | 请添加字幕文件开始")

    def _apply_theme(self) -> None:
        self.theme.apply_theme()
        # QSS 切换后重新应用自定义背景
        QTimer.singleShot(50, self._apply_custom_bg)

    def _apply_custom_bg(self) -> None:
        s = AppSettings()
        if s.bg_color or s.bg_image or s.bg_opacity != 100:
            self.theme.apply_custom_background(
                color=s.bg_color,
                opacity=s.bg_opacity,
                image_path=s.bg_image,
            )

    def _connect_signals(self) -> None:
        # 文件列表变化 → 启用/禁用翻译按钮
        self.file_panel.files_changed.connect(self._on_files_changed)
        # 分析面板确认按钮（只连接一次，避免重复）
        self.analysis_panel.approve_clicked.connect(self._on_analysis_approve_clicked)
        self.analysis_panel.approve_all_clicked.connect(self._on_analysis_approve_all)
        self.analysis_panel.optimize_glossary.connect(self._on_optimize_glossary)
        # 预览面板：单条重译
        self.preview_panel.retranslate_requested.connect(self._on_retranslate_entry)
        # 预览面板：并行多文件时切换查看
        self.preview_panel.file_selected.connect(self._on_preview_file_selected)
        # 语料库：从当前分析结果导入术语
        self.corpus_panel.import_btn.clicked.connect(self._on_corpus_import)

    def _on_files_changed(self, files: list[str]) -> None:
        has_subtitle = any(
            Path(fp).suffix.lower() in SUPPORTED_FORMATS for fp in files
        )
        has_media = any(Path(fp).suffix.lower() in MEDIA_FORMATS for fp in files)
        # 提取/翻译互斥期间保持按钮禁用，防止中途启动第二种任务
        self.translate_btn.setEnabled(has_subtitle and not self._extracting and not self._translating)
        self.extract_btn.setEnabled(has_media and not self._extracting and not self._translating)
        self.export_btn.setEnabled(len(files) > 0)
        if not files:
            # 清空列表时同步清掉预览状态，避免残留过期数据
            self._current_entries.clear()
            self._current_results.clear()
            self._current_file = ""
            self._active_files.clear()
            self.preview_panel.clear()
            self.save_edits_btn.setEnabled(False)
        total_kb = 0
        for fp in files:
            try:
                total_kb += Path(fp).stat().st_size
            except OSError:
                pass
        size_str = f" | 共 {total_kb / 1024:.0f} KB" if files else ""
        self.status_bar.showMessage(f"已加载 {len(files)} 个文件{size_str} | 就绪")

    # ---- 翻译流程 ----

    def _on_translate(self) -> None:
        """启动翻译（视频/音频文件不能直接翻译，需先提取字幕）"""
        checked = self.file_panel.get_checked_filepaths()
        if not checked:
            QMessageBox.warning(self, "未选择文件", "请先添加并勾选要翻译的字幕文件。")
            return
        files = [fp for fp in checked if Path(fp).suffix.lower() in SUPPORTED_FORMATS]
        media = [fp for fp in checked if Path(fp).suffix.lower() in MEDIA_FORMATS]
        if not files:
            if media:
                QMessageBox.information(
                    self, "需要先提取字幕",
                    "所选的文件是视频/音频，不能直接翻译。\n"
                    "请先勾选它们并点击「✎ 提取字幕」，识别出字幕后会自动加入列表。",
                )
            else:
                QMessageBox.warning(self, "未选择文件", "请先添加并勾选要翻译的字幕文件。")
            return

        # 检查 API Key
        provider = self.settings.provider
        api_key = self.settings.get_api_key(provider)
        if not api_key:
            QMessageBox.warning(
                self, "未配置 API 密钥",
                f"请先在设置中配置 {provider} 的 API 密钥。\n菜单: 设置 → 设置..."
            )
            return

        self.translate_btn.setEnabled(False)
        self.extract_btn.setEnabled(False)
        self._translating = True
        self.progress_widget.set_running(True)
        self.status_bar.showMessage("正在翻译...")
        # 清除上一个文件残留的分析面板（本次分析失败时也不会显示过期数据）
        self.analysis_panel.clear()

        # 1. 创建 translator 并准备 worker（此时 signals 已可访问）
        self.translator = TranslatorFacade()
        self.translator.prepare_translation(files)

        # 2. 断开旧信号，连接新信号
        for conn in self._translator_connections:
            try:
                conn.disconnect()
            except Exception:
                pass
        self._translator_connections.clear()

        s = self.translator.signals
        if s:
            self._translator_connections = [
                s.file_started.connect(self._on_file_started),
                s.file_analysis_done.connect(self._on_analysis_done),
                s.waiting_for_approval.connect(self._on_waiting_approval),
                s.approval_timed_out.connect(self._on_approval_timed_out),
                s.analysis_approved.connect(self._on_analysis_approved),
                s.batch_started.connect(self._on_batch_started),
                s.batch_completed.connect(self._on_batch_completed),
                s.batch_qc_failed.connect(self._on_batch_qc_failed),
                s.batch_retry.connect(self._on_batch_retry),
                s.file_completed.connect(self._on_file_completed),
                s.file_failed.connect(self._on_file_failed),
                s.progress_updated.connect(self._on_progress_updated),
                s.log_message.connect(self._on_log),
                s.all_completed.connect(self._on_all_completed),
            ]

        # 3. 信号全连上后才启动线程
        self.translator.start()

        # 安全兜底：活动感知看门狗，超时强制恢复 UI
        self._arm_safety_timer()

    def _arm_safety_timer(self) -> None:
        """（重新）武装安全计时器——仅在真正翻译进行时计时，
        等待用户确认分析时不计（用户控制节奏，不是卡死）"""
        self._stop_safety_timer()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(self._on_safety_timeout)
        timer.start(10 * 60 * 1000)
        self._safety_timer = timer

    def _on_safety_timeout(self) -> None:
        if self.translate_btn and not self.translate_btn.isEnabled():
            log.warning("翻译超时——10 分钟无批次完成，取消后台线程并恢复 UI")
            # 只复位 UI 不取消的话，worker 会继续跑完并写输出文件——
            # 用户随即重开翻译就是双流水线互写同一批 _zh 文件。
            # 不阻塞等待（同取消按钮）
            self.translator.cancel_no_wait()
            self._translating = False
            self.progress_widget.set_running(False)
            self.translate_btn.setEnabled(True)
            self._on_files_changed(self.file_panel.get_filepaths())
            self.status_bar.showMessage("翻译超时，请检查网络和 API 配置后重试")

    # ---- 字幕提取流程（faster-whisper 语音识别）----

    def _on_extract(self) -> None:
        """启动字幕提取：勾选的视频/音频 → whisper 识别 → 同目录 .srt"""
        checked = self.file_panel.get_checked_filepaths()
        media = [fp for fp in checked if Path(fp).suffix.lower() in MEDIA_FORMATS]
        if not media:
            QMessageBox.information(
                self, "没有可提取的文件",
                "请先添加并勾选视频/音频文件（mp4、mkv、mp3、wav 等）。",
            )
            return

        # 可用性检查按引擎分派：本地要 faster-whisper，在线要 openai SDK + Key
        if self.settings.asr_engine == "online":
            from core.asr_online import check_online_asr_available
            err = check_online_asr_available()
            if err is None and not self.settings.get_asr_api_key():
                QMessageBox.warning(
                    self, "缺少 API Key",
                    "在线语音识别需要 API Key。\n"
                    "菜单: 设置 → 字幕提取 → 识别引擎选「在线」→ 填入 API Key。",
                )
                return
        else:
            err = check_whisper_available()
        if err:
            QMessageBox.warning(
                self, "缺少依赖",
                f"所选识别引擎的依赖未安装或不可用：\n{err}\n\n"
                "本地引擎请执行: pip install faster-whisper\n"
                "在线引擎请执行: pip install openai",
            )
            return

        self.translate_btn.setEnabled(False)
        self.extract_btn.setEnabled(False)
        self._extracting = True
        self.progress_widget.set_running(True)
        self.status_bar.showMessage(f"正在提取字幕（{len(media)} 个文件）...")

        self.extractor = ExtractionFacade()
        self.extractor.prepare_extraction(media)

        for conn in self._extract_connections:
            try:
                conn.disconnect()
            except Exception:
                pass
        self._extract_connections.clear()

        s = self.extractor.signals
        self._extract_connections = [
            s.extraction_started.connect(self._on_extraction_started),
            s.model_loading.connect(self._on_model_loading),
            s.extraction_progress.connect(self._on_extraction_progress),
            s.extraction_completed.connect(self._on_extraction_completed),
            s.extraction_failed.connect(self._on_extraction_failed),
            s.all_completed.connect(self._on_extraction_all_completed),
        ]

        self.extractor.start()

    def _on_extraction_started(self, filepath: str) -> None:
        self.file_panel.update_file_status(filepath, "提取中", "0%")
        self.progress_widget.set_progress(0, 1, f"正在识别: {Path(filepath).name}")

    def _on_model_loading(self, desc: str) -> None:
        # 首次使用会从 HuggingFace 下载模型（进度显示在下方进度条），之后本地缓存复用
        self.status_bar.showMessage(
            f"正在准备识别模型 {desc}（首次使用需下载，进度见下方进度条）..."
        )

    def _on_extraction_progress(self, filepath: str, cur_ms: int, total_ms: int, text: str) -> None:
        pct = int(cur_ms * 100 / total_ms) if total_ms > 0 else 0
        pct = min(pct, 100)
        self.file_panel.update_file_status(filepath, "提取中", f"{pct}%")
        snippet = (text[:24] + "…") if len(text) > 24 else text
        self.progress_widget.set_progress(
            min(cur_ms, max(total_ms, 1)), max(total_ms, 1),
            f"提取中 {Path(filepath).name} {pct}% — {snippet}",
        )

    def _on_extraction_completed(self, filepath: str, srt_path: str, meta: dict) -> None:
        self.file_panel.update_file_status(filepath, "✓ 已提取", "100%")
        lang_names = {"ja": "日语", "en": "英语"}
        lang = (meta or {}).get("language", "")
        lang_name = lang_names.get(lang, f"语言 {lang}" if lang else "未知语言")
        note = "，时间轴为估算值（模型未返回时间戳，建议用带时间轴的模型重提取）" \
            if (meta or {}).get("timeline_estimated") else ""
        # 生成的字幕自动加入列表（勾选状态），可直接「开始翻译」
        self.file_panel.add_files([srt_path])
        self.status_bar.showMessage(
            f"提取完成: {Path(srt_path).name}（识别语言: {lang_name}）{note}，已加入列表，可直接翻译"
        )

    def _on_extraction_failed(self, filepath: str, error: str) -> None:
        self.file_panel.update_file_status(filepath, "✗ 失败")
        self.status_bar.showMessage(f"提取失败: {Path(filepath).name} — {error}")
        log.error("字幕提取失败: %s - %s", filepath, error)

    def _on_extraction_all_completed(self, results: dict[str, str]) -> None:
        self._extracting = False
        self.progress_widget.set_running(False)
        self._on_files_changed(self.file_panel.get_filepaths())
        success = sum(1 for v in results.values() if v)
        failed = len(results) - success
        if success and not failed:
            self.status_bar.showMessage("提取完成 | 全部成功")
        else:
            self.status_bar.showMessage(f"提取结束 | 成功: {success}, 失败: {failed}")

    def _on_corpus_changed(self) -> None:
        """语料库变更——如果翻译正在等待确认，提示用户重新确认"""
        pass  # corpus 变更后，下次分析/翻译会自动引用最新语料库

    def _on_corpus_import(self) -> None:
        """把当前分析结果的术语表合并进语料库（不覆盖已有词条）"""
        analysis = self.analysis_panel.analysis
        glossary = analysis.glossary if analysis else {}
        if not glossary:
            QMessageBox.information(
                self, "无可导入术语",
                "当前没有分析结果，或分析结果没有生成术语表。\n请先翻译一个文件并完成全篇分析。",
            )
            return
        before = len(self.corpus_panel.get_terms())
        self.corpus_panel.import_from_glossary(glossary)
        after = len(self.corpus_panel.get_terms())
        self.status_bar.showMessage(f"已从分析导入 {after - before} 个术语到语料库")

    def _on_file_started(self, filepath: str) -> None:
        self._current_file = filepath
        self._current_results[filepath] = {}
        if filepath not in self._active_files:
            self._active_files.append(filepath)
            self.preview_panel.set_file_list(self._active_files)
        self.file_panel.update_file_status(filepath, "分析中")
        # 提前加载原始字幕，后续分批翻译时逐步更新预览。
        # 解析放后台线程：pysubs2 全量解析 + 编码回退链同步跑会卡住 GUI
        # 线程（且与 worker 里的解析重复），审计 #77
        self._current_entries[filepath] = []  # 先占位，后台加载完成回填

        from PyQt5.QtCore import QThreadPool, QRunnable

        holder = _WorkerDone()
        holder.done.connect(
            lambda payload: self._on_entries_loaded(filepath, payload[0], payload[1])
        )

        class _LoadEntriesTask(QRunnable):
            def __init__(self, fp, holder):
                super().__init__()
                self.filepath = fp
                self.holder = holder

            def run(self):
                err = None
                entries = []
                try:
                    sub_file = SubtitleFile()
                    entries = sub_file.load(self.filepath)
                except Exception as e:
                    err = str(e)
                # 跨线程回调：pyqtSignal 的 queued 连接可靠投递到主线程
                self.holder.done.emit((entries, err))

        QThreadPool.globalInstance().start(_LoadEntriesTask(filepath, holder))

    def _on_entries_loaded(self, filepath: str, entries: list, err) -> None:
        """后台加载字幕完成——仅当该文件仍为当前预览文件时才刷新预览"""
        if err:
            log.warning("后台加载字幕失败 %s: %s", filepath, err)
            self._current_entries[filepath] = []
            return
        self._current_entries[filepath] = entries
        if filepath == self._current_file:
            self.preview_panel.load_entries(
                filepath, entries, self._current_results.get(filepath, {})
            )

    def _on_preview_file_selected(self, filepath: str) -> None:
        """并行翻译时用户切换查看的文件"""
        if filepath in self._current_entries:
            self._current_file = filepath
            self._refresh_preview()

    def _on_analysis_done(self, filepath: str, analysis) -> None:
        self.analysis_panel.show_analysis(filepath, analysis)
        self.file_panel.update_file_status(filepath, "待确认分析")

    def _on_waiting_approval(self, filepath: str) -> None:
        self.progress_widget.set_progress(0, 1, "等待确认分析结果")
        self.status_bar.showMessage("分析完成 — 请在左侧面板修改分析结果，然后点击「确认继续翻译」")
        # 等待用户确认期间暂停安全计时器（用户控制节奏，非卡死）
        self._stop_safety_timer()

    def _on_analysis_approved(self, filepath: str) -> None:
        self.file_panel.update_file_status(filepath, "翻译中")
        self._arm_safety_timer()

    def _on_approval_timed_out(self, filepath: str) -> None:
        """worker 确认等待 300s 超时：面板复位并恢复计时，不再卡在等待态
        （此前该信号未接线，面板永远停留「等待确认」，点击确认也被忽略）"""
        self.file_panel.update_file_status(filepath, "翻译中（确认超时）")
        self.analysis_panel.approve_btn.setEnabled(False)
        self.progress_widget.set_running(True)
        self.status_bar.showMessage(
            f"{Path(filepath).name} 分析确认超时，已使用原始分析继续翻译"
        )
        self._arm_safety_timer()

    def _refresh_preview(self) -> None:
        """增量更新预览面板（仅当前活动文件）"""
        entries = self._current_entries.get(self._current_file)
        if entries is not None:
            self.preview_panel.load_entries(
                self._current_file,
                entries,
                self._current_results.get(self._current_file, {}),
            )

    def _on_analysis_approve_clicked(self, analysis) -> None:
        self.analysis_panel.approve_btn.setEnabled(False)
        self.status_bar.showMessage("翻译中...")
        # 按分析面板当前展示的文件路由到对应 worker
        filepath = self.analysis_panel.current_filepath
        self.translator.approve_analysis(filepath, analysis)

    def _on_analysis_approve_all(self) -> None:
        """确认全部：当前文件带修改确认，其余文件自动确认"""
        self.status_bar.showMessage("已确认全部，正在翻译...")
        filepath = self.analysis_panel.current_filepath
        analysis = self.analysis_panel.analysis
        self.translator.approve_all(filepath, analysis)

    def _on_optimize_glossary(self, analysis: AnalysisResult) -> None:
        """触发术语深度优化（异步，不阻塞 UI）"""
        self.status_bar.showMessage("正在深度优化术语...")

        from PyQt5.QtCore import QThreadPool, QRunnable

        holder = _WorkerDone()
        holder.done.connect(self._on_glossary_optimized)

        class _OptimizeTask(QRunnable):
            def __init__(self, analyzer, glossary, entries, llm_creator, holder):
                super().__init__()
                self.analyzer = analyzer
                self.glossary = dict(glossary)
                self.entries = list(entries)
                self.llm_creator = llm_creator
                self.holder = holder

            def run(self):
                try:
                    prompt = self.analyzer.build_glossary_optimize_prompt(
                        self.glossary, self.entries
                    )
                    llm = self.llm_creator()
                    response = llm.analyze(prompt)
                    optimized = self.analyzer.parse_glossary_optimize_response(response)
                except Exception as e:
                    optimized = {"_error": str(e)}
                # 跨线程回调：pyqtSignal 的 queued 连接可靠投递到主线程
                self.holder.done.emit(optimized)

        pool = QThreadPool.globalInstance()
        task = _OptimizeTask(
            SubtitleAnalyzer(),
            analysis.glossary,
            self._current_entries.get(self._current_file, []),
            lambda: self._get_llm_service(),
            holder,
        )
        pool.start(task)

    def _on_glossary_optimized(self, optimized: dict) -> None:
        if "_error" in optimized:
            self.status_bar.showMessage(f"术语优化失败: {optimized['_error']}")
        else:
            self.analysis_panel.on_glossary_optimized(optimized)
            self.status_bar.showMessage(f"术语优化完成，共 {len(optimized)} 个术语")
        self.analysis_panel.optimize_gloss_btn.setEnabled(True)
        self.analysis_panel.optimize_gloss_btn.setText("深度优化术语")

    def _get_llm_service(self):
        from services.openai_service import OpenAIService
        from services.claude_service import ClaudeService
        from config.settings import AppSettings
        s = AppSettings()
        if s.provider == "anthropic":
            return ClaudeService()
        return OpenAIService()

    def _on_batch_started(self, filepath: str, batch_id: int, total: int) -> None:
        self.file_panel.update_file_status(filepath, "翻译中", f"{batch_id + 1}/{total}")
        self._arm_safety_timer()

    def _on_batch_completed(self, filepath: str, batch_id: int, results) -> None:
        results_map = self._current_results.setdefault(filepath, {})
        for r in results:
            results_map[r.index] = r
        self.save_edits_btn.setEnabled(True)
        if filepath == self._current_file:
            # 就地更新变化行，避免每批整表销毁重建（审计 #59）
            self.preview_panel.update_results(results)

    def _on_batch_qc_failed(self, filepath: str, batch_id: int, results) -> None:
        results_map = self._current_results.setdefault(filepath, {})
        for r in results:
            results_map[r.index] = r
        if filepath == self._current_file:
            self.preview_panel.update_results(results)

    def _on_batch_retry(self, filepath: str, batch_id: int, retry_num: int) -> None:
        self.status_bar.showMessage(f"批次 {batch_id} 质检未通过，重试第 {retry_num} 次...")

    def _on_file_completed(self, filepath: str, output_path: str) -> None:
        self.file_panel.update_file_status(filepath, "✓ 完成")

        # 如果刚完成的文件是当前选中的，加载预览
        if filepath == self._current_file or not self._current_file:
            self._load_preview(filepath)

        self.status_bar.showMessage(f"完成: {Path(filepath).name}")

    def _on_file_failed(self, filepath: str, error: str) -> None:
        # 只标记该文件失败——整体收尾由 all_completed 统一处理，
        # 避免并行时一个文件失败就把整轮 UI 重置、让用户误启动第二轮
        self.file_panel.update_file_status(filepath, "✗ 失败")
        self.status_bar.showMessage(f"失败: {Path(filepath).name} — {error}")
        log.error("文件翻译失败: %s - %s", filepath, error)

    def _on_progress_updated(self, filepath: str, current: int, total: int, status: str) -> None:
        self.progress_widget.set_progress(current, total, status)

    def _on_log(self, msg: str) -> None:
        self.status_bar.showMessage(msg)

    def _on_all_completed(self, results: dict[str, str]) -> None:
        self._stop_safety_timer()
        self._translating = False
        self.progress_widget.set_running(False)
        self.translate_btn.setEnabled(True)
        self._on_files_changed(self.file_panel.get_filepaths())
        success = sum(1 for v in results.values() if v)
        failed = len(results) - success
        self.status_bar.showMessage(f"翻译完成 | 成功: {success}, 失败: {failed}")

    def _on_cancel(self) -> None:
        self._stop_safety_timer()
        if self._extracting:
            self.extractor.cancel()
            self._extracting = False
            self.progress_widget.set_running(False)
            self._on_files_changed(self.file_panel.get_filepaths())
            self.status_bar.showMessage("已取消提取（当前识别调用结束后停止）")
        else:
            # 不阻塞等待：wait(3000)×N 会让 UI 冻结最长 N×3 秒，
            # 线程由 _live_threads 注册表兜底，worker 已收到取消事件
            self.translator.cancel_no_wait()
            self._translating = False
            self.progress_widget.set_running(False)
            self.translate_btn.setEnabled(True)
            self._on_files_changed(self.file_panel.get_filepaths())
            self.status_bar.showMessage("已取消")

    def _stop_safety_timer(self) -> None:
        if hasattr(self, '_safety_timer') and self._safety_timer.isActive():
            self._safety_timer.stop()

    def _load_preview(self, filepath: str) -> None:
        """加载翻译结果到预览面板（复用翻译开始时缓存的条目，避免重复解析）"""
        try:
            entries = self._current_entries.get(filepath)
            if not entries:
                # 后台加载未完成或失败时兜底同步解析（源文件翻译期间不会变化）
                sub_file = SubtitleFile()
                entries = sub_file.load(filepath)
                self._current_entries[filepath] = entries
            self._current_file = filepath
            self.preview_panel.load_entries(
                filepath, entries, self._current_results.get(filepath, {})
            )
        except Exception as e:
            log.exception("加载预览失败: %s", e)

    # ---- 单条重译 & 编辑持久化 ----

    def _on_retranslate_entry(self, index: int) -> None:
        """右键「重新翻译此条」：在后台线程单条重译"""
        entries = self._current_entries.get(self._current_file, [])
        entry = next((e for e in entries if e.index == index), None)
        if not entry:
            return
        if not self.settings.get_api_key(self.settings.provider):
            QMessageBox.warning(self, "未配置 API 密钥", "请先在设置中配置 API 密钥。")
            return

        analysis = self.analysis_panel.analysis
        glossary = analysis.glossary if analysis else {}

        # 复用主流水线的标签占位符机制：重译前先把内联 ASS/SRT 标签换成
        # <TAG_N> 占位符送给 LLM，译文回填时才能还原标签。entry.text 是
        # 剥离标签后的纯文本，直接发出去会让标签在重译后永久丢失（审计 #53）
        from core.merger import Merger
        th = Merger().tag_handler
        cleaned_text, _ = th.extract_tags(entry.original_text)
        batch_entry = SubtitleEntry(
            index=entry.index,
            start=entry.start,
            end=entry.end,
            text=cleaned_text,
            original_text=entry.original_text,
            style=entry.style,
        )
        batch = TranslationBatch(
            batch_id=-1,
            entries=[batch_entry],
            primary_start_idx=0,
            primary_end_idx=1,
            glossary=glossary,
        )

        # 源语言=自动检测时按整篇内容判定（与主流水线一致，审计 #64）
        source_lang = self.settings.source_lang
        if source_lang == "auto":
            from core.analyzer import detect_source_lang
            source_lang = detect_source_lang(entries)
        target_lang = self.settings.target_lang

        from PyQt5.QtCore import QThreadPool, QRunnable

        holder = _WorkerDone()
        # 请求时刻锁定归属文件：回调按回调时刻的 _current_file 取归属的话，
        # 并行翻译中用户切换预览文件会让重译结果写进另一文件的 results
        # 并随自动保存落盘（数据串文件）
        request_filepath = self._current_file
        holder.done.connect(
            lambda payload: self._on_entry_retranslated(
                request_filepath, index, payload[0], payload[1]
            )
        )

        class _RetranslateTask(QRunnable):
            def __init__(self, b, src, tgt, llm_factory, holder):
                super().__init__()
                self.batch = b
                self.src = src
                self.tgt = tgt
                self.llm_factory = llm_factory
                self.holder = holder

            def run(self):
                text, err = None, None
                try:
                    llm = self.llm_factory()
                    out = llm.translate_batch(self.batch, self.src, self.tgt)
                    text = out[0] if out else None
                except Exception as e:
                    err = str(e)
                self.holder.done.emit((text, err))

        self.status_bar.showMessage(f"正在重新翻译第 {index + 1} 条...")
        task = _RetranslateTask(
            batch,
            source_lang,
            target_lang,
            self._get_llm_service,
            holder,
        )
        QThreadPool.globalInstance().start(task)

    def _on_entry_retranslated(self, filepath: str, index: int, text: str | None, err: str | None) -> None:
        if err or not text:
            self.status_bar.showMessage(f"重新翻译失败: {err or '空译文'}")
            return
        # 回填内联标签：重译请求发的是 <TAG_N> 占位符，这里还原成原标签
        # （占位符未还原说明 LLM 丢弃了它，退回原文保住标签，审计 #53）
        entries = self._current_entries.get(filepath, [])
        entry = next((e for e in entries if e.index == index), None)
        if entry:
            from core.merger import Merger
            th = Merger().tag_handler
            _, tag_map = th.extract_tags(entry.original_text)
            restored = th.restore_tags(text, tag_map)
            if "<TAG_" in restored:
                restored = entry.original_text
            text = restored
        results = self._current_results.setdefault(filepath, {})
        if index in results:
            r = results[index]
            r.translated_text = text
            r.status = "manually_edited"
            r.qc_issues = []
        else:
            orig_entry = next((e for e in entries if e.index == index), None)
            results[index] = TranslationResult(
                index=index,
                original_text=orig_entry.text if orig_entry else "",
                translated_text=text,
                status="manually_edited",
            )
        # 结果归属 filepath：仅当用户仍停留在该文件时刷新预览
        if self._current_file == filepath:
            self._refresh_preview()
        # 自动保存防抖：连续多次单条重译合并为一次整文件写入，
        # 避免 N 次重译 = N 次全文件解析 + 全量写（审计 #78）
        self._schedule_auto_save()
        self.status_bar.showMessage(f"已重新翻译第 {index + 1} 条，稍后自动保存到文件")

    def _schedule_auto_save(self) -> None:
        """重译/编辑后的自动保存做防抖合并，避免逐条整文件重写"""
        if not hasattr(self, "_auto_save_timer"):
            self._auto_save_timer = QTimer()
            self._auto_save_timer.setSingleShot(True)
            self._auto_save_timer.timeout.connect(self._do_auto_save)
        self._auto_save_timer.start(800)

    def _do_auto_save(self) -> None:
        """防抖定时器到点：执行一次自动保存"""
        if not self._current_file:
            return
        if self._resave_current_file():
            self.status_bar.showMessage("已自动保存重译/编辑结果到 _zh 文件")
        else:
            self.status_bar.showMessage("自动保存失败（可用「保存修改」重试）")

    def closeEvent(self, event) -> None:
        # 防抖定时器未到点就退出时立即落盘，避免丢失最后一次重译结果
        if getattr(self, "_auto_save_timer", None) and self._auto_save_timer.isActive():
            self._auto_save_timer.stop()
            self._do_auto_save()
        # 翻译/提取进行中关闭窗口：先取消后台线程（置取消事件 + quit/wait），
        # 否则解释器拆卸时销毁仍在运行的 QThread 会触发
        # "QThread: Destroyed while thread is still running" 硬崩溃
        if getattr(self, "_translating", False):
            self.translator.cancel()
        if getattr(self, "_extracting", False):
            self.extractor.cancel()
        super().closeEvent(event)

    def _resave_current_file(self) -> bool:
        """把当前文件的内存结果合并并写回 _zh 文件（让重译/编辑持久化）"""
        if not self._current_file:
            return False
        entries = self._current_entries.get(self._current_file)
        results = self._current_results.get(self._current_file, {})
        if not entries:
            return False
        try:
            from core.merger import Merger
            from core.subtitle_io import SubtitleFile
            from core.translation_batch import SubtitleEntry
            th = Merger().tag_handler
            merged: list[SubtitleEntry] = []
            for e in entries:
                r = results.get(e.index)
                trans_text = r.translated_text if r else e.original_text
                _, tag_map = th.extract_tags(e.original_text)
                final = th.restore_tags(trans_text, tag_map)
                # 占位符未还原 → 退回原文
                if "<TAG_" in final:
                    final = e.original_text
                merged.append(
                    SubtitleEntry(
                        index=e.index, start=e.start, end=e.end,
                        text=final, original_text=e.original_text, style=e.style,
                    )
                )
            p = Path(self._current_file)
            out = p.parent / f"{p.stem}_zh{p.suffix}"
            SubtitleFile().save(str(out), merged, self._current_file)
            return True
        except Exception as e:
            log.exception("保存修改失败: %s", e)
            return False

    # ---- 导出 ----

    def _on_save_edits(self) -> None:
        """把预览中的人工修改/重译结果写回 _zh 文件"""
        if not self._current_file:
            return
        # 显式保存时取消待触发的防抖自动保存（避免重复写）
        if getattr(self, "_auto_save_timer", None) and self._auto_save_timer.isActive():
            self._auto_save_timer.stop()
        if self._resave_current_file():
            p = Path(self._current_file)
            out = p.parent / f"{p.stem}_zh{p.suffix}"
            self.status_bar.showMessage(f"已保存修改到: {out}")
        else:
            QMessageBox.warning(self, "保存失败", "没有可保存的内容，或写入失败。")

    def _on_export(self) -> None:
        files = self.file_panel.get_checked_filepaths()
        if not files:
            QMessageBox.warning(self, "未选择文件", "请先添加并勾选要导出的字幕文件。")
            return

        out_dir = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if not out_dir:
            return

        exported = 0
        for fp in files:
            # 翻译后的文件在源文件同目录，带 _zh 后缀
            p = Path(fp)
            translated = p.parent / f"{p.stem}_zh{p.suffix}"
            if translated.exists():
                dest = Path(out_dir) / translated.name
                shutil.copy2(translated, dest)
                exported += 1

        if exported > 0:
            QMessageBox.information(
                self, "导出完成",
                f"已导出 {exported} 个翻译文件到:\n{out_dir}"
            )
        else:
            QMessageBox.information(
                self, "提示",
                "未找到翻译后的文件。\n请先翻译字幕，翻译结果会自动保存在源文件同目录（文件名_zh）。"
            )

    # ---- 对话框 ----

    def _on_settings(self) -> None:
        dlg = SettingsDialog(self)
        dlg.exec_()

    def _on_entry_double_clicked(self, result: TranslationResult) -> None:
        dlg = EditorDialog(result, self)
        dlg.exec_()
        # 对话框内修改可能改了结果对象，刷新预览让改动可见
        self._refresh_preview()

    def _on_about(self) -> None:
        QMessageBox.about(
            self, "关于",
            f"<b>{APP_NAME}</b> v{APP_VERSION}<br><br>"
            "基于 LLM 的字幕自动翻译工具。<br>"
            "支持 SRT/ASS/VTT 格式，日/英→中文翻译。<br>"
            "内置 faster-whisper 语音识别，可从视频/音频直接提取日/英字幕。<br><br>"
            f"Powered by OpenAI / Anthropic / faster-whisper"
        )
