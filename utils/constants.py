"""应用常量定义"""

APP_NAME = "字幕翻译工具"
APP_VERSION = "1.0.0"
APP_ORG = "SubtitleTranslator"

SUPPORTED_FORMATS = {
    ".srt": "SubRip (SRT)",
    ".ass": "Advanced SubStation Alpha (ASS)",
    ".ssa": "SubStation Alpha (SSA)",
    ".vtt": "WebVTT (VTT)",
}

# 字幕提取（语音识别）支持的媒体输入格式
MEDIA_FORMATS = {
    ".mp4": "视频 (MP4)",
    ".mkv": "视频 (MKV)",
    ".avi": "视频 (AVI)",
    ".mov": "视频 (MOV)",
    ".wmv": "视频 (WMV)",
    ".flv": "视频 (FLV)",
    ".webm": "视频 (WebM)",
    ".m4v": "视频 (M4V)",
    ".ts": "视频 (TS)",
    ".mp3": "音频 (MP3)",
    ".wav": "音频 (WAV)",
    ".m4a": "音频 (M4A)",
    ".aac": "音频 (AAC)",
    ".flac": "音频 (FLAC)",
    ".ogg": "音频 (OGG)",
    ".opus": "音频 (OPUS)",
    ".wma": "音频 (WMA)",
}

SOURCE_LANGUAGES = {
    "ja": "日语",
    "en": "英语",
    "auto": "自动检测",
}

TARGET_LANGUAGES = {
    "zh-CN": "简体中文",
    "zh-TW": "繁体中文",
}

LLM_PROVIDERS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic (Claude)",
    "deepseek": "DeepSeek",
    "custom": "自定义 OpenAI 兼容 API",
}

# 各模型的默认上下文窗口大小 (tokens)
MODEL_CONTEXT_WINDOWS = {
    # OpenAI
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "gpt-4.1": 1000000,
    "gpt-4.1-mini": 1000000,
    "gpt-4.1-nano": 1000000,
    "gpt-4-turbo": 128000,
    "gpt-5": 400000,
    "gpt-5-mini": 400000,
    # Anthropic — Claude 5 家族
    "claude-fable-5": 1000000,      # 旗舰（1M 上下文变体）
    "claude-opus-5": 200000,
    "claude-sonnet-5": 200000,
    "claude-haiku-4-5-20251001": 200000,
    # Anthropic — Claude 4 家族（兼容旧配置）
    "claude-sonnet-4-20250514": 200000,
    "claude-opus-4-20250514": 200000,
    "claude-haiku-4-20250514": 200000,
    # DeepSeek V4
    "deepseek-v4-flash": 1000000,
    "deepseek-v4-pro": 1000000,
    # 旧名兼容（即将弃用，分别映射到 v4-flash 的非思考/思考模式）
    "deepseek-chat": 1000000,
    "deepseek-reasoner": 1000000,
    "deepseek-v3-0324": 131072,
}
# 所有 provider 的默认上下文窗口（通用兜底值）
DEFAULT_PROVIDER_CONTEXT = {
    "openai": 400000,
    "anthropic": 200000,
    "deepseek": 1000000,
    "custom": 65536,
}

DEFAULT_MODELS = {
    "openai": "gpt-5-mini",           # 成本友好 + 能力足够
    "anthropic": "claude-sonnet-5",   # 最新默认
    "deepseek": "deepseek-v4-flash",
    "custom": "",
}

# 各 provider 的默认 API 端点
DEFAULT_API_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "",
    "deepseek": "https://api.deepseek.com",
    "custom": "",
}

# 默认设置值
DEFAULT_CONTEXT_LIMIT = 64000      # 上下文窗口上限
DEFAULT_CHUNK_TOKENS = 2000        # 每批目标 token 数
DEFAULT_ANALYSIS_TOKENS = 16000    # 分析阶段预留 token
DEFAULT_OVERLAP_ENTRIES = 5        # overlap 条目数
DEFAULT_TEMPERATURE = 0.3          # LLM 温度
DEFAULT_MAX_RETRIES = 3            # API 最大重试
DEFAULT_MAX_QC_RETRIES = 2         # 质检最大重试
DEFAULT_PARALLEL_FILES = 2         # 并行翻译文件数（多 QThread worker）
DEFAULT_ANALYSIS_MODE = "smart"    # smart=智能取样 / full=全文发送
DEFAULT_SOURCE_LANG = "ja"
DEFAULT_TARGET_LANG = "zh-CN"

# 字幕拆分断点
SCENE_BREAK_GAP_MS = 5000          # 场景间隔阈值（毫秒）

# ---- 字幕提取（faster-whisper 语音识别）----

# 模型选项：key 为 faster-whisper 的 model_size_or_path（也可填本地路径/其他 HF 仓库）
WHISPER_MODELS = {
    "large-v3-turbo": "large-v3-turbo — 推荐，日/英识别强且速度快",
    "large-v3": "large-v3 — 最高精度，速度较慢",
    "medium": "medium — 精度/速度平衡（约 1.5GB）",
    "small": "small — 较快，精度一般（约 460MB）",
    "base": "base — 快速验证用（约 140MB）",
    "tiny": "tiny — 最快，草稿用（约 75MB）",
    "kotoba-tech/kotoba-whisper-v2.0-faster": "kotoba-whisper-v2.0 — 日语专用优化模型",
}

DEFAULT_WHISPER_MODEL = "large-v3-turbo"
DEFAULT_WHISPER_DEVICE = "auto"    # auto / cpu / cuda
DEFAULT_WHISPER_COMPUTE = "auto"   # auto / int8 / float16 / float32
DEFAULT_WHISPER_VAD = True         # VAD 过滤静音段（提升长直播识别质量）
DEFAULT_WHISPER_THREADS = 0        # CPU 线程数，0 = 自动
DEFAULT_EXTRACT_LANG = "auto"      # 提取语言：auto / ja / en

# 模型下载端点："" = 官方 huggingface.co；国内网络建议选镜像
HF_MIRRORS = {
    "": "默认（huggingface.co）",
    "https://hf-mirror.com": "hf-mirror.com（国内镜像）",
}

# ---- 在线语音识别 ----

# 识别引擎：本地离线 / 在线 API
ASR_ENGINES = {
    "local": "本地 faster-whisper（离线，音频不上传）",
    "online": "在线语音识别 API（速度快，免下载模型）",
}
DEFAULT_ASR_ENGINE = "local"

# 在线服务预设。protocol:
#   "transcriptions" = OpenAI 兼容 /v1/audio/transcriptions（文件直传，
#                      verbose_json 带时间轴，字幕质量最佳）
#   "chat_audio"     = Chat Completions + input_audio（MiMo 式，base64 传输，
#                      文本输出无时间轴 → 按时长均分估算）
# max_mb: 单次请求音频大小上限（超限自动按时间切块分次识别）
ASR_SERVICES = {
    "openai": (
        "OpenAI 官方",
        "https://api.openai.com/v1",
        ["whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"],
        "transcriptions", 25,
    ),
    "groq": (
        "Groq（免费额度，速度快）",
        "https://api.groq.com/openai/v1",
        ["whisper-large-v3", "whisper-large-v3-turbo"],
        "transcriptions", 25,
    ),
    "siliconflow": (
        "硅基流动 SiliconFlow（国内）",
        "https://api.siliconflow.cn/v1",
        ["FunAudioLLM/SenseVoiceSmall", "Tencent/FireRedASR-AED-L"],
        "transcriptions", 25,
    ),
    "mimo": (
        "小米 MiMo（中英/方言）",
        "https://api.xiaomimimo.com/v1",
        ["mimo-v2.5-asr"],
        "chat_audio", 10,
    ),
    "custom": ("自定义 OpenAI 兼容", "", [], "transcriptions", 25),
}
DEFAULT_ASR_SERVICE = "openai"
DEFAULT_ASR_MODEL = "whisper-1"
