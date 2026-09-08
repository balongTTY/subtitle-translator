"""设置对话框 — API 密钥 / 模型 / 语言 / 翻译选项"""

import re

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget,
    QWidget, QFormLayout, QLineEdit, QComboBox,
    QSpinBox, QDoubleSpinBox, QPushButton, QSlider,
    QLabel, QMessageBox, QGroupBox, QCheckBox,
    QColorDialog, QFileDialog,
)
from PyQt5.QtCore import Qt as QtCore, QObject, QRunnable, QThreadPool, pyqtSignal
from PyQt5.QtGui import QColor

from services.openai_service import OpenAIService
from services.claude_service import ClaudeService

from config.settings import AppSettings
from utils.constants import (
    LLM_PROVIDERS, SOURCE_LANGUAGES, TARGET_LANGUAGES,
    MODEL_CONTEXT_WINDOWS, DEFAULT_MODELS, DEFAULT_PROVIDER_CONTEXT,
    DEFAULT_API_BASE_URLS,
    WHISPER_MODELS, HF_MIRRORS, ASR_ENGINES, ASR_SERVICES,
)


class _ValidateSignalHolder(QObject):
    """验证结果回传信号（queued 到主线程；对话框持有引用防 GC）"""

    done = pyqtSignal(bool, str)  # (ok, message)

# 各 provider 的常用模型列表
PROVIDER_MODELS = {
    "openai": ["gpt-5", "gpt-5-mini", "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4-turbo"],
    "anthropic": ["claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001",
                  "claude-sonnet-4-20250514", "claude-opus-4-20250514", "claude-haiku-4-20250514"],
    "deepseek": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
    "custom": [],
}

# 各 provider 的 API Key 提示
API_KEY_HINTS = {
    "openai": "sk-...",
    "anthropic": "sk-ant-...",
    "deepseek": "sk-...",
    "custom": "在此粘贴 API Key",
}

API_KEY_LABELS = {
    "openai": "OpenAI API Key:",
    "anthropic": "Claude API Key:",
    "deepseek": "DeepSeek API Key:",
    "custom": "API Key:",
}


class SettingsDialog(QDialog):
    """设置对话框"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.settings = AppSettings()
        self._setup_ui()
        self._load_settings()

    def _setup_ui(self) -> None:
        self.setWindowTitle("设置")
        self.setMinimumSize(600, 620)

        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()

        # Tab 1: API 密钥
        self.tabs.addTab(self._create_api_tab(), "API 密钥")

        # Tab 2: 模型
        self.tabs.addTab(self._create_model_tab(), "模型")

        # Tab 3: 语言
        self.tabs.addTab(self._create_language_tab(), "语言")

        # Tab 4: 翻译选项
        self.tabs.addTab(self._create_translate_tab(), "翻译选项")

        # Tab 5: 外观
        self.tabs.addTab(self._create_appearance_tab(), "外观")

        # Tab 6: 字幕提取
        self.tabs.addTab(self._create_extract_tab(), "字幕提取")

        layout.addWidget(self.tabs)

        # 底部按钮
        btn_layout = QHBoxLayout()
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._on_test_connection)
        btn_layout.addWidget(self.test_btn)
        btn_layout.addStretch()

        ok_btn = QPushButton("确定")
        ok_btn.clicked.connect(self._on_ok)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)

        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    # ------------------------------------------------------------------
    # Tab 1: API 密钥
    # ------------------------------------------------------------------

    def _create_api_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self.provider_combo = QComboBox()
        for key, name in LLM_PROVIDERS.items():
            self.provider_combo.addItem(name, key)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow("LLM 提供商:", self.provider_combo)

        # 各 provider 的 key 输入框（用 StackedWidget 切换）
        self.key_label = QLabel("API Key:")
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.Password)
        self.key_input.setPlaceholderText("在此粘贴 API Key")
        form.addRow(self.key_label, self.key_input)

        # 自定义 API 端点
        self.endpoint_label = QLabel("API 端点:")
        self.endpoint_input = QLineEdit()
        self.endpoint_input.setPlaceholderText("https://api.example.com/v1")
        form.addRow(self.endpoint_label, self.endpoint_input)

        return w

    # ------------------------------------------------------------------
    # Tab 2: 模型
    # ------------------------------------------------------------------

    def _create_model_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)  # 允许手动输入模型名
        self.model_combo.setInsertPolicy(QComboBox.NoInsert)
        form.addRow("模型:", self.model_combo)

        self.context_presets = QComboBox()
        self.context_presets.addItem("4K", 4096)
        self.context_presets.addItem("8K", 8192)
        self.context_presets.addItem("16K", 16384)
        self.context_presets.addItem("32K", 32768)
        self.context_presets.addItem("64K", 65536)
        self.context_presets.addItem("128K", 131072)
        self.context_presets.addItem("200K", 200000)
        self.context_presets.addItem("1M", 1000000)
        self.context_presets.addItem("自定义", -1)
        self.context_presets.currentIndexChanged.connect(self._on_preset_changed)
        form.addRow("上下文预设:", self.context_presets)

        self.context_limit = QSpinBox()
        self.context_limit.setRange(1024, 2000000)
        self.context_limit.setSingleStep(1000)
        self.context_limit.setSuffix(" tokens")
        self.context_limit.valueChanged.connect(self._on_context_manual_edit)
        self.context_hint = QLabel()
        self.context_hint.setStyleSheet("color: #888; font-size: 8pt;")
        ctx_layout = QHBoxLayout()
        ctx_layout.addWidget(self.context_limit)
        ctx_layout.addWidget(self.context_hint)
        ctx_layout.addStretch()
        form.addRow(" 自定义数值:", ctx_layout)

        self.model_combo.currentTextChanged.connect(self._update_context_hint)

        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.1)
        self.temperature.setDecimals(1)
        form.addRow("温度:", self.temperature)

        self.max_retries = QSpinBox()
        self.max_retries.setRange(1, 10)
        form.addRow("最大重试:", self.max_retries)

        return w

    # ------------------------------------------------------------------
    # Tab 3: 语言
    # ------------------------------------------------------------------

    def _create_language_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self.source_lang = QComboBox()
        for key, name in SOURCE_LANGUAGES.items():
            self.source_lang.addItem(name, key)
        form.addRow("源语言:", self.source_lang)

        self.target_lang = QComboBox()
        for key, name in TARGET_LANGUAGES.items():
            self.target_lang.addItem(name, key)
        form.addRow("目标语言:", self.target_lang)

        return w

    # ------------------------------------------------------------------
    # Tab 4: 翻译选项
    # ------------------------------------------------------------------

    def _create_translate_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        group = QGroupBox("分块设置")
        form = QFormLayout(group)

        self.chunk_presets = QComboBox()
        self.chunk_presets.addItem("500 tokens", 500)
        self.chunk_presets.addItem("1000 tokens", 1000)
        self.chunk_presets.addItem("2000 tokens", 2000)
        self.chunk_presets.addItem("3000 tokens", 3000)
        self.chunk_presets.addItem("5000 tokens", 5000)
        self.chunk_presets.addItem("自定义", -1)
        self.chunk_presets.currentIndexChanged.connect(self._on_chunk_preset_changed)
        form.addRow("每批目标大小:", self.chunk_presets)

        self.chunk_tokens = QSpinBox()
        self.chunk_tokens.setRange(100, 64000)
        self.chunk_tokens.setSingleStep(100)
        self.chunk_tokens.setSuffix(" tokens")
        self.chunk_tokens.valueChanged.connect(self._on_chunk_manual_edit)
        form.addRow("  自定义:", self.chunk_tokens)

        self.overlap_presets = QComboBox()
        self.overlap_presets.addItem("0 条 (无重叠)", 0)
        self.overlap_presets.addItem("3 条", 3)
        self.overlap_presets.addItem("5 条 (推荐)", 5)
        self.overlap_presets.addItem("10 条", 10)
        self.overlap_presets.addItem("自定义", -1)
        self.overlap_presets.currentIndexChanged.connect(self._on_overlap_preset_changed)
        form.addRow("Overlap 条数:", self.overlap_presets)

        self.overlap_entries = QSpinBox()
        self.overlap_entries.setRange(0, 30)
        self.overlap_entries.valueChanged.connect(self._on_overlap_manual_edit)
        form.addRow("  自定义:", self.overlap_entries)

        self.analysis_presets = QComboBox()
        self.analysis_presets.addItem("4K", 4096)
        self.analysis_presets.addItem("8K", 8192)
        self.analysis_presets.addItem("16K", 16384)
        self.analysis_presets.addItem("32K", 32768)
        self.analysis_presets.addItem("64K", 65536)
        self.analysis_presets.addItem("128K", 131072)
        self.analysis_presets.addItem("1M", 1000000)
        self.analysis_presets.addItem("自定义", -1)
        self.analysis_presets.currentIndexChanged.connect(self._on_analysis_preset_changed)
        form.addRow("分析阶段预留:", self.analysis_presets)

        self.analysis_tokens = QSpinBox()
        self.analysis_tokens.setRange(1000, 2000000)
        self.analysis_tokens.setSingleStep(1000)
        self.analysis_tokens.setSuffix(" tokens")
        self.analysis_tokens.valueChanged.connect(self._on_analysis_manual_edit)
        form.addRow("  自定义:", self.analysis_tokens)

        self.analysis_mode = QComboBox()
        self.analysis_mode.addItem("全文发送（1M 上下文模型推荐）", "full")
        self.analysis_mode.addItem("智能取样（短上下文兼容）", "smart")
        form.addRow("分析模式:", self.analysis_mode)

        self.parallel_files = QSpinBox()
        self.parallel_files.setRange(1, 8)
        self.parallel_files.setToolTip("多个文件同时翻译（共享同一速率限制，不会超发 API 配额）")
        form.addRow("并行文件数:", self.parallel_files)

        layout.addWidget(group)

        group2 = QGroupBox("质检")
        form2 = QFormLayout(group2)

        self.max_qc_retries = QSpinBox()
        self.max_qc_retries.setRange(0, 5)
        form2.addRow("质检最大重试:", self.max_qc_retries)

        layout.addWidget(group2)
        layout.addStretch()
        return w

    # ------------------------------------------------------------------
    # Provider 切换
    # ------------------------------------------------------------------

    def _on_provider_changed(self) -> None:
        provider = self.provider_combo.currentData()

        # 更新 API Key 标签和提示
        self.key_label.setText(API_KEY_LABELS.get(provider, "API Key:"))
        self.key_input.setPlaceholderText(API_KEY_HINTS.get(provider, ""))

        # 加载该 provider 的已保存 key
        saved_key = self.settings.get_api_key(provider)
        if saved_key:
            self.key_input.setPlaceholderText("已设置 (••••••••)")
        self.key_input.clear()

        # 更新模型列表，选中该 provider 上次保存的模型
        self.model_combo.clear()
        models = PROVIDER_MODELS.get(provider, [])
        default_model = DEFAULT_MODELS.get(provider, "")
        saved_model = self.settings.get(f"model_{provider}", default_model)
        for m in models:
            ctx = MODEL_CONTEXT_WINDOWS.get(m, DEFAULT_PROVIDER_CONTEXT.get(provider, 65536))
            label = f"{m} (上限 {ctx // 1000}k)"
            self.model_combo.addItem(label, m)
        if saved_model:
            idx = self.model_combo.findData(saved_model)
            if idx >= 0:
                self.model_combo.setCurrentIndex(idx)
            elif models:
                self.model_combo.setCurrentText(saved_model)

        if not models:
            self.model_combo.setEditable(True)
            self.model_combo.setCurrentText(saved_model or "")
            self.model_combo.setPlaceholderText("输入模型名称...")
        elif saved_model and self.model_combo.currentIndex() < 0:
            # findData 没找到但用户之前可能存了带后缀的值，直接用原始名
            self.model_combo.setCurrentText(saved_model)

        # 更新端点输入（直接读该 provider 的存储，不通过 property）
        default_url = DEFAULT_API_BASE_URLS.get(provider, "")
        saved_url = self.settings.get(f"base_url_{provider}", default_url)
        if saved_url and saved_url != default_url:
            self.endpoint_input.setText(saved_url)
            self.endpoint_input.setPlaceholderText(default_url or "https://api.example.com/v1")
        else:
            self.endpoint_input.setText(default_url)
            self.endpoint_input.setPlaceholderText(default_url or "https://api.example.com/v1")

        self._update_context_hint()

    def _update_context_hint(self) -> None:
        provider = self.provider_combo.currentData()
        model = self.model_combo.currentData() or self.model_combo.currentText()
        max_ctx = MODEL_CONTEXT_WINDOWS.get(model) or DEFAULT_PROVIDER_CONTEXT.get(provider, 65536)
        self.context_hint.setText(f"(模型上限: {max_ctx // 1000}k tokens)")

    def _on_preset_changed(self) -> None:
        self._apply_preset(self.context_presets, self.context_limit)

    def _on_context_manual_edit(self) -> None:
        self._sync_preset_to_custom(self.context_limit, self.context_presets)

    # ---- 翻译选项预设处理器 ----

    def _on_chunk_preset_changed(self) -> None:
        self._apply_preset(self.chunk_presets, self.chunk_tokens)

    def _on_chunk_manual_edit(self) -> None:
        self._sync_preset_to_custom(self.chunk_tokens, self.chunk_presets)

    def _on_overlap_preset_changed(self) -> None:
        self._apply_preset(self.overlap_presets, self.overlap_entries)

    def _on_overlap_manual_edit(self) -> None:
        self._sync_preset_to_custom(self.overlap_entries, self.overlap_presets)

    def _on_analysis_preset_changed(self) -> None:
        self._apply_preset(self.analysis_presets, self.analysis_tokens)

    def _on_analysis_manual_edit(self) -> None:
        self._sync_preset_to_custom(self.analysis_tokens, self.analysis_presets)

    # ---- 通用预设工具方法 ----

    def _apply_preset(self, preset_combo: QComboBox, spinbox: QSpinBox) -> None:
        value = preset_combo.currentData()
        if value > 0:
            spinbox.setValue(value)

    def _get_clean_model_name(self) -> str:
        """获取干净的模型名（去除显示后缀）"""
        data = self.model_combo.currentData()
        if data:
            return data
        text = self.model_combo.currentText().strip()
        # 移除末尾的 " (上限 XXXk)" 或 " (上限 XXXk tokens)" 等后缀
        cleaned = re.sub(r'\s*\(.*?\)\s*$', '', text)
        return cleaned.strip()

    def _sync_preset_to_custom(self, spinbox: QSpinBox, preset_combo: QComboBox) -> None:
        current_val = spinbox.value()
        for i in range(preset_combo.count()):
            if preset_combo.itemData(i) == current_val:
                preset_combo.blockSignals(True)
                preset_combo.setCurrentIndex(i)
                preset_combo.blockSignals(False)
                return
        preset_combo.blockSignals(True)
        preset_combo.setCurrentIndex(preset_combo.count() - 1)
        preset_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Tab 5: 外观
    # ------------------------------------------------------------------

    def _create_appearance_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        group = QGroupBox("背景设置")
        form = QFormLayout(group)

        # 背景颜色
        color_layout = QHBoxLayout()
        self.bg_color_btn = QPushButton()
        self.bg_color_btn.setFixedSize(36, 24)
        self.bg_color_btn.setStyleSheet("border: 1px solid #ccc; border-radius: 4px;")
        self.bg_color_btn.clicked.connect(self._pick_bg_color)
        self.bg_color_label = QLabel("未设置")
        color_layout.addWidget(self.bg_color_btn)
        color_layout.addWidget(self.bg_color_label)
        color_layout.addStretch()
        color_layout.addWidget(QPushButton("清除"))
        # 清除按钮
        color_layout.itemAt(color_layout.count() - 1).widget().clicked.connect(
            lambda: self._set_bg_color("")
        )
        form.addRow("背景颜色:", color_layout)

        # 透明度
        opacity_layout = QHBoxLayout()
        self.bg_opacity_slider = QSlider(QtCore.Horizontal)
        self.bg_opacity_slider.setRange(30, 100)
        self.bg_opacity_slider.setValue(100)
        self.bg_opacity_slider.setTickPosition(QSlider.TicksBelow)
        self.bg_opacity_slider.setTickInterval(10)
        self.bg_opacity_label = QLabel("100%")
        self.bg_opacity_slider.valueChanged.connect(
            lambda v: self.bg_opacity_label.setText(f"{v}%")
        )
        opacity_layout.addWidget(QLabel("30%"))
        opacity_layout.addWidget(self.bg_opacity_slider)
        opacity_layout.addWidget(QLabel("100%"))
        opacity_layout.addWidget(self.bg_opacity_label)
        form.addRow("窗口透明度:", opacity_layout)

        # 背景图片
        img_layout = QHBoxLayout()
        self.bg_image_path = QLineEdit()
        self.bg_image_path.setPlaceholderText("选择背景图片...")
        self.bg_image_path.setReadOnly(True)
        img_layout.addWidget(self.bg_image_path)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._pick_bg_image)
        img_layout.addWidget(browse_btn)
        clear_img_btn = QPushButton("清除")
        clear_img_btn.clicked.connect(lambda: self.bg_image_path.setText(""))
        img_layout.addWidget(clear_img_btn)
        form.addRow("背景图片:", img_layout)

        layout.addWidget(group)

        # 预览按钮
        preview_btn = QPushButton("预览效果")
        preview_btn.clicked.connect(self._preview_appearance)
        reset_btn = QPushButton("恢复默认")
        reset_btn.clicked.connect(self._reset_appearance)
        btn_row = QHBoxLayout()
        btn_row.addWidget(preview_btn)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)
        layout.addStretch()
        return w

    def _pick_bg_color(self) -> None:
        color = QColorDialog.getColor(QColor(self.settings.bg_color) if self.settings.bg_color else QtCore.white, self, "选择背景颜色")
        if color.isValid():
            self._set_bg_color(color.name())

    def _set_bg_color(self, hex_color: str) -> None:
        if hex_color:
            self.settings.bg_color = hex_color
            self.bg_color_btn.setStyleSheet(
                f"background: {hex_color}; border: 1px solid #ccc; border-radius: 4px;"
            )
            self.bg_color_label.setText(hex_color)
        else:
            self.settings.bg_color = ""
            self.bg_color_btn.setStyleSheet("border: 1px solid #ccc; border-radius: 4px;")
            self.bg_color_label.setText("未设置")

    def _pick_bg_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择背景图片", "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif);;所有文件 (*.*)"
        )
        if path:
            self.bg_image_path.setText(path)

    def _preview_appearance(self) -> None:
        from gui.theme_manager import ThemeManager
        tm = ThemeManager()
        tm.apply_custom_background(
            color=self.settings.bg_color,
            opacity=self.bg_opacity_slider.value(),
            image_path=self.bg_image_path.text(),
        )

    def _reset_appearance(self) -> None:
        self._set_bg_color("")
        self.settings.bg_opacity = 100
        self.settings.bg_image = ""
        self.bg_opacity_slider.setValue(100)
        self.bg_image_path.setText("")
        from gui.theme_manager import ThemeManager
        ThemeManager().clear_custom_background()

    # ------------------------------------------------------------------
    # Tab 6: 字幕提取（本地 faster-whisper / 在线 API）
    # ------------------------------------------------------------------

    def _create_extract_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # ---- 识别引擎 ----
        self.asr_engine = QComboBox()
        for key, name in ASR_ENGINES.items():
            self.asr_engine.addItem(name, key)
        self.asr_engine.currentIndexChanged.connect(self._on_asr_engine_changed)
        layout.addWidget(QLabel("识别引擎:"))
        layout.addWidget(self.asr_engine)

        self.extract_lang = QComboBox()
        for key, name in SOURCE_LANGUAGES.items():
            self.extract_lang.addItem(name, key)
        self.extract_lang.setToolTip("选「自动检测」时每个文件单独判定语言")
        layout.addWidget(QLabel("识别语言:"))
        layout.addWidget(self.extract_lang)

        # ---- 本地引擎组 ----
        self.local_group = QGroupBox("本地 faster-whisper")
        form = QFormLayout(self.local_group)

        self.whisper_model = QComboBox()
        self.whisper_model.setEditable(True)  # 可填本地路径或其他 HF 仓库名
        self.whisper_model.setInsertPolicy(QComboBox.NoInsert)
        for value, label in WHISPER_MODELS.items():
            self.whisper_model.addItem(label, value)
        form.addRow("Whisper 模型:", self.whisper_model)

        self.whisper_device = QComboBox()
        self.whisper_device.addItem("自动", "auto")
        self.whisper_device.addItem("CPU", "cpu")
        self.whisper_device.addItem("CUDA (NVIDIA GPU)", "cuda")
        form.addRow("运行设备:", self.whisper_device)

        self.whisper_compute = QComboBox()
        for v, name in [
            ("auto", "自动"), ("int8", "int8（CPU 推荐）"),
            ("float16", "float16（GPU 推荐）"), ("float32", "float32（最高精度）"),
        ]:
            self.whisper_compute.addItem(name, v)
        form.addRow("计算精度:", self.whisper_compute)

        self.whisper_threads = QSpinBox()
        self.whisper_threads.setRange(0, 64)
        self.whisper_threads.setSpecialValueText("自动")
        self.whisper_threads.setToolTip("CPU 推理线程数，0 = 自动")
        form.addRow("CPU 线程数:", self.whisper_threads)

        self.whisper_vad = QCheckBox("VAD 过滤静音段（长直播推荐开启）")
        self.whisper_vad.setChecked(True)
        form.addRow("", self.whisper_vad)

        self.hf_mirror = QComboBox()
        for value, label in HF_MIRRORS.items():
            self.hf_mirror.addItem(label, value)
        self.hf_mirror.setToolTip("模型从 HuggingFace 下载；国内网络建议选镜像")
        form.addRow("模型下载源:", self.hf_mirror)

        # 一键下载/检查模型 + 进度条
        from PyQt5.QtWidgets import QProgressBar
        from PyQt5.QtCore import QThreadPool, QRunnable, QObject, pyqtSignal
        dl_row = QHBoxLayout()
        self.model_dl_btn = QPushButton("⬇ 下载 / 检查模型")
        self.model_dl_btn.setToolTip("把当前选中的模型预先下载到本地（也可在首次提取时自动下载）")
        self.model_dl_btn.clicked.connect(self._on_download_model)
        self.model_dl_bar = QProgressBar()
        self.model_dl_bar.setVisible(False)
        self.model_dl_bar.setTextVisible(True)
        self.model_dl_bar.setFormat("%p%")
        dl_row.addWidget(self.model_dl_btn)
        dl_row.addWidget(self.model_dl_bar, 1)
        form.addRow("", dl_row)

        local_hint = QLabel(
            "模型仅首次使用时下载并缓存（large-v3-turbo 约 1.6GB），之后离线可用；"
            "音频不出本机。"
        )
        local_hint.setWordWrap(True)
        local_hint.setStyleSheet("color: #888; font-size: 8pt;")
        form.addRow("", local_hint)
        layout.addWidget(self.local_group)

        # ---- 在线引擎组 ----
        self.online_group = QGroupBox("在线语音识别 API")
        online_form = QFormLayout(self.online_group)

        self.asr_service = QComboBox()
        for key, (name, _url, _models, _proto, _mb) in ASR_SERVICES.items():
            self.asr_service.addItem(name, key)
        self.asr_service.currentIndexChanged.connect(self._on_asr_service_changed)
        online_form.addRow("在线服务:", self.asr_service)

        self.asr_base_url_input = QLineEdit()
        self.asr_base_url_input.setPlaceholderText("https://api.example.com/v1")
        online_form.addRow("API 端点:", self.asr_base_url_input)

        self.asr_api_key_input = QLineEdit()
        self.asr_api_key_input.setEchoMode(QLineEdit.Password)
        self.asr_api_key_input.setPlaceholderText("在此粘贴 API Key")
        online_form.addRow("API Key:", self.asr_api_key_input)

        key_row = QHBoxLayout()
        self.asr_test_btn = QPushButton("测试 Key")
        self.asr_test_btn.clicked.connect(self._on_test_asr_key)
        key_row.addStretch()
        key_row.addWidget(self.asr_test_btn)
        online_form.addRow("", key_row)

        self.asr_model = QComboBox()
        self.asr_model.setEditable(True)
        self.asr_model.setInsertPolicy(QComboBox.NoInsert)
        online_form.addRow("识别模型:", self.asr_model)

        online_hint = QLabel(
            "whisper 系模型返回带时间轴的分段（推荐）；文字型模型（如 MiMo、"
            "gpt-4o-transcribe）只返回文本，时间轴按时长均分估算。\n"
            "长音频会自动按大小上限切块分次识别（音频将上传到所配置的服务）。"
        )
        online_hint.setWordWrap(True)
        online_hint.setStyleSheet("color: #888; font-size: 8pt;")
        online_form.addRow("", online_hint)
        layout.addWidget(self.online_group)

        layout.addStretch()

        # 表单行数多，小屏幕下允许滚动（滚动容器包住整个内容区）
        from PyQt5.QtWidgets import QScrollArea
        scroll = QScrollArea()
        scroll.setWidget(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        return scroll

    def _on_asr_engine_changed(self) -> None:
        online = self.asr_engine.currentData() == "online"
        self.local_group.setVisible(not online)
        self.online_group.setVisible(online)

    def _on_asr_service_changed(self) -> None:
        """切换在线服务：填默认端点并重填模型预设（保留当前选择若仍可用）"""
        key = self.asr_service.currentData()
        name, url, models, _proto, _mb = ASR_SERVICES.get(key, ("", "", [], "", 25))
        if key != "custom":
            self.asr_base_url_input.setText(url)
        else:
            self.asr_base_url_input.setPlaceholderText("https://api.example.com/v1")
        current = self.asr_model.currentText().strip()
        self.asr_model.clear()
        for m in models:
            self.asr_model.addItem(m, m)
        if current:
            idx = self.asr_model.findData(current)
            if idx >= 0:
                self.asr_model.setCurrentIndex(idx)
            elif not models or key == "custom":
                self.asr_model.setCurrentText(current)
        elif models:
            self.asr_model.setCurrentIndex(0)

    def _combo_value(self, combo: QComboBox) -> str:
        """取组合框当前值：选中预设项时用 data；用户手改文本时以文本为准。

        旧逻辑 currentData() or currentText() 在「选中预设项后又编辑了显示文本」
        时仍返回旧 data——用户改了字却存回旧模型名，表现为「选不了模型」。
        """
        idx = combo.currentIndex()
        if idx >= 0 and combo.currentText() == combo.itemText(idx):
            return str(combo.currentData() or combo.currentText()).strip()
        return combo.currentText().strip()

    # ---- 模型一键下载 ----

    def _on_download_model(self) -> None:
        """下载/检查当前选中的 whisper 模型（后台线程 + 进度条）"""
        from core.model_download import (
            download_model_with_progress, is_model_cached, model_size_hint,
        )
        model = self._combo_value(self.whisper_model)
        if not model:
            QMessageBox.warning(self, "未选择模型", "请先在上方选择或输入要下载的模型。")
            return

        cached = is_model_cached(model)
        if cached:
            QMessageBox.information(
                self, "模型已就绪",
                f"「{model}」已在本地缓存，无需下载：\n{cached}",
            )
            return

        hint = model_size_hint(model)
        ret = QMessageBox.question(
            self, "下载模型",
            f"将下载「{model}」（{hint}）到本地缓存。\n继续？",
        )
        if ret != QMessageBox.Yes:
            return

        # 后台线程下载，进度经 pyqtSignal 队列投递回主线程
        from PyQt5.QtCore import QThreadPool, QRunnable, QObject, pyqtSignal

        mirror = self.hf_mirror.currentData() or ""

        class _Sig(QObject):
            progress = pyqtSignal(int, int)   # done, total
            finished = pyqtSignal(object)     # {"ok": path} | {"error": str}

        class _Task(QRunnable):
            def __init__(self, sig):
                super().__init__()
                self.sig = sig

            def run(self):
                try:
                    path = download_model_with_progress(
                        model,
                        hf_mirror=mirror,
                        on_progress=lambda d, t: self.sig.progress.emit(d, t),
                    )
                    self.sig.finished.emit({"ok": path})
                except Exception as e:
                    self.sig.finished.emit({"error": str(e)})

        sig = _Sig()
        sig.progress.connect(self._on_model_dl_progress)
        sig.finished.connect(self._on_model_dl_finished)
        self.model_dl_btn.setEnabled(False)
        self.model_dl_btn.setText("下载中…")
        self.model_dl_bar.setVisible(True)
        self.model_dl_bar.setRange(0, 100)
        self.model_dl_bar.setValue(0)
        QThreadPool.globalInstance().start(_Task(sig))

    def _on_model_dl_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.model_dl_bar.setValue(int(done * 100 / total))

    def _on_model_dl_finished(self, result: dict) -> None:
        self.model_dl_btn.setEnabled(True)
        self.model_dl_btn.setText("⬇ 下载 / 检查模型")
        if "ok" in result:
            self.model_dl_bar.setValue(100)
            QMessageBox.information(
                self, "下载完成", f"模型已就绪：\n{result['ok']}",
            )
        else:
            self.model_dl_bar.setVisible(False)
            QMessageBox.critical(self, "下载失败", result.get("error", "未知错误"))


    def _on_test_asr_key(self) -> None:
        """用当前输入的端点/Key/模型构造临时客户端验证（不写入全局设置）"""
        from core.asr_online import OnlineASR
        key = self.asr_api_key_input.text().strip() or self.settings.get_asr_api_key()
        if not key:
            QMessageBox.warning(self, "缺少 API Key", "请先填入在线识别 API Key。")
            return
        try:
            asr = OnlineASR(
                base_url=self.asr_base_url_input.text().strip(),
                api_key=key,
                model=self._combo_value(self.asr_model) or "whisper-1",
                protocol=ASR_SERVICES.get(self.asr_service.currentData(), (0, 0, 0, "transcriptions", 0))[3],
                max_upload_mb=ASR_SERVICES.get(self.asr_service.currentData(), (0, 0, 0, 0, 25))[4],
            )
        except ValueError as e:
            QMessageBox.warning(self, "配置不完整", str(e))
            return
        self._validate_in_background(self.asr_test_btn, asr)

    # ------------------------------------------------------------------
    # 加载/保存
    # ------------------------------------------------------------------

    def _load_settings(self) -> None:
        s = self.settings

        idx = self.provider_combo.findData(s.provider)
        if idx >= 0:
            self.provider_combo.setCurrentIndex(idx)

        # _on_provider_changed 被触发后会填充模型列表，然后选中当前模型
        model_idx = self.model_combo.findData(s.model)
        if model_idx >= 0:
            self.model_combo.setCurrentIndex(model_idx)
        elif s.model:
            self.model_combo.setCurrentText(s.model)

        self.context_limit.setValue(s.context_limit)
        self.temperature.setValue(s.temperature)
        self.max_retries.setValue(s.max_retries)

        src_idx = self.source_lang.findData(s.source_lang)
        if src_idx >= 0:
            self.source_lang.setCurrentIndex(src_idx)
        tgt_idx = self.target_lang.findData(s.target_lang)
        if tgt_idx >= 0:
            self.target_lang.setCurrentIndex(tgt_idx)

        self.chunk_tokens.setValue(s.chunk_tokens)
        self.overlap_entries.setValue(s.overlap_entries)
        self.analysis_tokens.setValue(s.analysis_tokens)
        self.parallel_files.setValue(s.parallel_files)
        self.max_qc_retries.setValue(s.max_qc_retries)
        idx = self.analysis_mode.findData(s.analysis_mode)
        if idx >= 0:
            self.analysis_mode.setCurrentIndex(idx)

        # 外观设置
        if s.bg_color:
            self._set_bg_color(s.bg_color)
        self.bg_opacity_slider.setValue(s.bg_opacity)
        self.bg_image_path.setText(s.bg_image)

        # 字幕提取设置
        idx = self.whisper_model.findData(s.whisper_model)
        if idx >= 0:
            self.whisper_model.setCurrentIndex(idx)
        else:
            self.whisper_model.setCurrentText(s.whisper_model)
        idx = self.extract_lang.findData(s.extract_lang)
        if idx >= 0:
            self.extract_lang.setCurrentIndex(idx)
        idx = self.whisper_device.findData(s.whisper_device)
        if idx >= 0:
            self.whisper_device.setCurrentIndex(idx)
        idx = self.whisper_compute.findData(s.whisper_compute)
        if idx >= 0:
            self.whisper_compute.setCurrentIndex(idx)
        self.whisper_threads.setValue(s.whisper_threads)
        self.whisper_vad.setChecked(s.whisper_vad)
        idx = self.hf_mirror.findData(s.hf_mirror)
        if idx >= 0:
            self.hf_mirror.setCurrentIndex(idx)

        # 在线识别：先填服务预设（端点/模型列表），再用已存值覆盖
        self.asr_service.blockSignals(True)
        idx = self.asr_service.findData(s.asr_service)
        self.asr_service.setCurrentIndex(idx if idx >= 0 else 0)
        self.asr_service.blockSignals(False)
        self._on_asr_service_changed()
        if s.asr_base_url:
            self.asr_base_url_input.setText(s.asr_base_url)
        idx = self.asr_model.findData(s.asr_model)
        if idx >= 0:
            self.asr_model.setCurrentIndex(idx)
        elif s.asr_model:
            self.asr_model.setCurrentText(s.asr_model)
        if self.settings.get_asr_api_key():
            self.asr_api_key_input.setPlaceholderText("已设置 (••••••••)")

        idx = self.asr_engine.findData(s.asr_engine)
        self.asr_engine.blockSignals(True)
        self.asr_engine.setCurrentIndex(idx if idx >= 0 else 0)
        self.asr_engine.blockSignals(False)
        self._on_asr_engine_changed()

        # 加载自定义端点
        self.endpoint_input.setText(s.custom_base_url)

    def _on_ok(self) -> None:
        s = self.settings
        provider = self.provider_combo.currentData()

        # 保存 API key
        key = self.key_input.text().strip()
        if key:
            s.set_api_key(provider, key)

        # 先切 provider，再存自定义端点——custom_base_url setter 按
        # 当前 provider 决定存储键 base_url_{provider}，顺序反了会把
        # 端点存到旧 provider 名下导致静默丢失（审计 #49）
        s.provider = provider

        # 保存自定义端点
        endpoint = self.endpoint_input.text().strip()
        s.custom_base_url = endpoint

        model = self._get_clean_model_name()
        s.model = model
        s.context_limit = self.context_limit.value()
        s.temperature = self.temperature.value()
        s.max_retries = self.max_retries.value()
        s.source_lang = self.source_lang.currentData()
        s.target_lang = self.target_lang.currentData()
        s.chunk_tokens = self.chunk_tokens.value()
        s.overlap_entries = self.overlap_entries.value()
        s.analysis_tokens = self.analysis_tokens.value()
        s.parallel_files = self.parallel_files.value()
        s.max_qc_retries = self.max_qc_retries.value()
        s.analysis_mode = self.analysis_mode.currentData()
        s.bg_opacity = self.bg_opacity_slider.value()
        s.bg_image = self.bg_image_path.text()

        # 字幕提取设置
        s.whisper_model = self._combo_value(self.whisper_model)
        s.extract_lang = self.extract_lang.currentData()
        s.whisper_device = self.whisper_device.currentData()
        s.whisper_compute = self.whisper_compute.currentData()
        s.whisper_threads = self.whisper_threads.value()
        s.whisper_vad = self.whisper_vad.isChecked()
        s.hf_mirror = self.hf_mirror.currentData() or ""

        # 在线识别设置（Key 输入非空才写入——空输入保留已存 Key）
        s.asr_engine = self.asr_engine.currentData() or "local"
        s.asr_service = self.asr_service.currentData() or "openai"
        s.asr_base_url = self.asr_base_url_input.text().strip()
        s.asr_model = self._combo_value(self.asr_model) or "whisper-1"
        key = self.asr_api_key_input.text().strip()
        if key:
            s.set_asr_api_key(key)

        self.accept()

    def _on_test_connection(self) -> None:
        """测试 API 连接 — 用显式临时配置构造服务，不改动全局设置。

        旧实现把 provider/key/endpoint 临时写进全局单例 AppSettings：
        (1) 翻译在 worker 线程进行时，正在处理的批次可能读到测试配置发往
        错误 provider/模型（审计 #66）；(2) set_api_key 同步落盘，测试中
        进程被强杀会把未验证密钥残留磁盘，与 README「测试不会永久保存
        密钥」承诺相悖（审计 #67/#73）。现在服务支持显式配置参数，
        测试完全不触碰全局设置。
        """
        provider = self.provider_combo.currentData()
        key = self.key_input.text().strip()
        endpoint = self.endpoint_input.text().strip()

        # 输入框为空时回退到已保存的密钥（_on_provider_changed 会把已保存
        # key 以掩码占位符形式展示，输入框本身为空）
        if not key:
            key = self.settings.get_api_key(provider)
        if not key:
            QMessageBox.warning(
                self, "缺少 API 密钥",
                "请先在「API 密钥」页粘贴 API Key，再测试连接。",
            )
            return

        model = self._get_clean_model_name()
        if provider == "anthropic":
            svc = ClaudeService(provider=provider, api_key=key, base_url=endpoint, model=model)
        else:
            svc = OpenAIService(provider=provider, api_key=key, base_url=endpoint, model=model)

        self._validate_in_background(self.test_btn, svc)

    def _validate_in_background(self, btn, svc) -> None:
        """后台线程执行 validate_api_key——主线程同步网络请求会把整个
        设置对话框（乃至主窗口）冻结到超时为止"""
        orig_text = btn.text()
        btn.setEnabled(False)
        btn.setText("测试中…")
        holder = _ValidateSignalHolder()
        self._validate_holder = holder  # 持引用，防 queued 信号发射前被 GC
        holder.done.connect(
            lambda ok, msg, b=btn, t=orig_text: self._on_validate_done(ok, msg, b, t)
        )

        class _Task(QRunnable):
            def __init__(self, service, sig):
                super().__init__()
                self._svc = service
                self._sig = sig

            def run(self):
                try:
                    ok = self._svc.validate_api_key()
                    self._sig.done.emit(
                        ok,
                        "" if ok else "验证失败，请检查密钥/Key 与端点是否正确。",
                    )
                except Exception as e:
                    self._sig.done.emit(False, f"无法连接到 API:\n{e}")

        QThreadPool.globalInstance().start(_Task(svc, holder))

    def _on_validate_done(self, ok: bool, msg: str, btn, orig_text: str) -> None:
        btn.setEnabled(True)
        btn.setText(orig_text)
        if ok:
            QMessageBox.information(self, "连接成功", "API 连接正常！")
        else:
            QMessageBox.warning(self, "连接失败", msg)
