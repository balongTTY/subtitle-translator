"""应用设置管理 — QSettings + 密钥加密存储"""

import ctypes
import json
import logging
import os
import uuid
import hashlib
import base64
from ctypes import wintypes
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from PyQt5.QtCore import QSettings

from utils.constants import (
    APP_ORG,
    APP_NAME,
    DEFAULT_MODELS,
    DEFAULT_PROVIDER_CONTEXT,
    DEFAULT_CONTEXT_LIMIT,
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_ANALYSIS_TOKENS,
    DEFAULT_ANALYSIS_MODE,
    DEFAULT_OVERLAP_ENTRIES,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_QC_RETRIES,
    DEFAULT_PARALLEL_FILES,
    DEFAULT_SOURCE_LANG,
    DEFAULT_TARGET_LANG,
    DEFAULT_API_BASE_URLS,
    DEFAULT_WHISPER_MODEL,
    DEFAULT_WHISPER_DEVICE,
    DEFAULT_WHISPER_COMPUTE,
    DEFAULT_WHISPER_VAD,
    DEFAULT_WHISPER_THREADS,
    DEFAULT_EXTRACT_LANG,
    DEFAULT_ASR_ENGINE,
    DEFAULT_ASR_SERVICE,
    DEFAULT_ASR_MODEL,
)

log = logging.getLogger("subtitle_translator")


def _get_data_dir() -> Path:
    path = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_ORG
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
#  API 密钥存储：Windows 上优先用 DPAPI（当前用户域加密，非明文，无法跨机器/用户）
#  非 Windows 环境回退到「持久化随机密钥（0600 文件）」的 Fernet，
#  并删除明文密钥文件；MAC 派生密钥仅用于读取更早版本的历史文件
# ---------------------------------------------------------------------------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_DPAPI_INITIALIZED = False


def _init_dpapi() -> None:
    global _DPAPI_INITIALIZED
    if _DPAPI_INITIALIZED:
        return
    crypt32 = ctypes.windll.crypt32
    # 64 位下必须声明参数类型，否则指针被截断
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p,
        ctypes.POINTER(_DATA_BLOB), wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p,
        ctypes.POINTER(_DATA_BLOB), wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _DPAPI_INITIALIZED = True


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise NotImplementedError("DPAPI 仅支持 Windows")
    _init_dpapi()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    # 标志 0：CRYPTPROTECT_UI_FORBIDDEN，作用域为当前用户
    if not crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise NotImplementedError("DPAPI 仅支持 Windows")
    _init_dpapi()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _legacy_mac_fernet() -> Fernet | None:
    """更早版本用的 MAC 派生密钥 —— 仅用于读取历史 keys.enc（迁移用），不再用于新写入"""
    try:
        machine_id = str(uuid.getnode())
        salt = b"subtitle_translator_salt_2025"
        derived = hashlib.sha256(machine_id.encode() + salt).digest()
        return Fernet(base64.urlsafe_b64encode(derived))
    except Exception:
        return None


def _get_legacy_fernet() -> Fernet:
    """非 Windows 兜底加密密钥：持久化随机密钥（0600 文件）。

    旧实现用 MAC 地址 + 源码硬编码盐派生密钥——两个输入都半公开，本地进程可轻易还原；
    且在无硬件地址环境下 uuid.getnode() 回退为每进程随机值，导致重启后密钥漂移、
    已保存的 keys.enc 无法解密。改用 os.urandom 生成密钥并原子落盘（0600），
    密钥文件本身即秘密，重启后稳定可用。
    """
    key_file = _get_data_dir() / "fernet.key"
    try:
        raw = key_file.read_bytes()
        if len(raw) == 32:
            try:
                os.chmod(key_file, 0o600)
            except OSError:
                pass
            return Fernet(base64.urlsafe_b64encode(raw))
    except OSError:
        pass
    key = os.urandom(32)
    tmp = key_file.with_suffix(".tmp")
    tmp.write_bytes(key)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, key_file)
    return Fernet(base64.urlsafe_b64encode(key))


class AppSettings:
    """单例设置管理器"""

    _instance: "AppSettings | None" = None

    def __new__(cls) -> "AppSettings":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._qsettings = QSettings(APP_ORG, APP_NAME)
        self._keys_file = _get_data_dir() / "keys.enc"
        self._keys: dict[str, str] = {}
        self._load_keys()

    # ---- 带防护的数值读取 ----

    def _get_number(self, key: str, default, cast, *, lo=None, hi=None):
        """读取数值设置：非法值（手工编辑 INI/注册表写入的垃圾）回退默认，
        并按 [lo, hi] 钳制——裸 int()/float() 遇非数字垃圾直接 ValueError，
        chunk_tokens/temperature 每批翻译都会读取，损坏后流水线每次必崩"""
        raw = self.get(key, default)
        try:
            v = cast(raw)
        except (TypeError, ValueError):
            log.warning("设置 %s=%r 非法，回退默认 %s", key, raw, default)
            v = cast(default)
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    # ---- API Key 管理 ----

    def _load_keys(self) -> None:
        # 启动时无条件清理旧版明文密钥文件 .keyfile：旧版本以明文存放 Fernet 密钥，
        # 迁移完成后即为敏感残留。放在最前并独立 try，任何分支失败都不影响密钥加载。
        legacy_keyfile = _get_data_dir() / ".keyfile"
        if legacy_keyfile.exists():
            try:
                legacy_keyfile.unlink()
                log.info("已清理旧版明文密钥文件 .keyfile")
            except OSError as e:
                log.warning("清理旧版明文密钥文件 .keyfile 失败: %s", e)

        if not self._keys_file.exists():
            return
        raw = self._keys_file.read_bytes()

        # 1) 新格式：Windows DPAPI
        try:
            decrypted = _dpapi_unprotect(raw)
            self._keys = json.loads(decrypted)
            return
        except NotImplementedError:
            pass
        except Exception:
            # 可能是旧版 Fernet 格式，继续尝试迁移
            pass

        # 2) 旧格式：Fernet（仅迁移用）
        try:
            try:
                self._keys = json.loads(_get_legacy_fernet().decrypt(raw))
            except Exception:
                # 兼容更早版本：MAC 派生密钥加密的历史文件
                mac = _legacy_mac_fernet()
                if mac is None:
                    raise
                self._keys = json.loads(mac.decrypt(raw))
            log.info("检测到旧版密钥格式，正在迁移到新密钥存储")
            self._save_keys()
        except Exception as e:
            # 不静默丢数据：明确告警，用户重新输入即可（原文件保留在磁盘，不删除）
            log.warning("密钥文件解密失败，已保存的 API 密钥需要重新输入: %s", e)
            self._keys = {}

    def _save_keys(self) -> None:
        if os.name == "nt":
            encrypted = _dpapi_protect(json.dumps(self._keys).encode())
        else:
            # 非 Windows：用持久化随机密钥（0600 文件）加密，不落盘明文密钥
            encrypted = _get_legacy_fernet().encrypt(json.dumps(self._keys).encode())
        # 原子写入：先写临时文件再 rename，进程被杀不会留下截断的 keys.enc；
        # 同时收紧权限为 0600（非 Windows 上生效）
        tmp = self._keys_file.with_suffix(".tmp")
        tmp.write_bytes(encrypted)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._keys_file)

    def get_api_key(self, provider: str) -> str:
        return self._keys.get(provider, "")

    def set_api_key(self, provider: str, key: str) -> None:
        if key:
            self._keys[provider] = key
        else:
            self._keys.pop(provider, None)
        self._save_keys()

    # ---- 通用设置存取 ----

    def get(self, key: str, default: Any = None) -> Any:
        value = self._qsettings.value(key, default)
        if isinstance(default, bool) and not isinstance(value, bool):
            value = str(value).lower() == "true"
        return value

    def set(self, key: str, value: Any) -> None:
        self._qsettings.setValue(key, value)

    # ---- 便捷属性 ----

    @property
    def provider(self) -> str:
        return self.get("provider", "openai")

    @provider.setter
    def provider(self, value: str) -> None:
        self.set("provider", value)

    @property
    def model(self) -> str:
        provider = self.provider
        return self.get(f"model_{provider}", DEFAULT_MODELS.get(provider, "gpt-4o"))

    @model.setter
    def model(self, value: str) -> None:
        provider = self.provider
        self.set(f"model_{provider}", value)

    @property
    def context_limit(self) -> int:
        return self._get_number("context_limit", DEFAULT_CONTEXT_LIMIT, int, lo=1000)

    @context_limit.setter
    def context_limit(self, value: int) -> None:
        self.set("context_limit", value)

    @property
    def chunk_tokens(self) -> int:
        return self._get_number("chunk_tokens", DEFAULT_CHUNK_TOKENS, int, lo=200, hi=100000)

    @chunk_tokens.setter
    def chunk_tokens(self, value: int) -> None:
        self.set("chunk_tokens", value)

    @property
    def analysis_tokens(self) -> int:
        return self._get_number("analysis_tokens", DEFAULT_ANALYSIS_TOKENS, int, lo=256, hi=100000)

    @analysis_tokens.setter
    def analysis_tokens(self, value: int) -> None:
        self.set("analysis_tokens", value)

    @property
    def overlap_entries(self) -> int:
        return self._get_number("overlap_entries", DEFAULT_OVERLAP_ENTRIES, int, lo=0, hi=50)

    @overlap_entries.setter
    def overlap_entries(self, value: int) -> None:
        self.set("overlap_entries", value)

    @property
    def temperature(self) -> float:
        return self._get_number("temperature", DEFAULT_TEMPERATURE, float, lo=0.0, hi=2.0)

    @temperature.setter
    def temperature(self, value: float) -> None:
        self.set("temperature", value)

    @property
    def max_retries(self) -> int:
        # 下限 1：QSettings 无校验可能存到 0，重试循环 range(0) 一次都不执行
        return self._get_number("max_retries", DEFAULT_MAX_RETRIES, int, lo=1, hi=10)

    @max_retries.setter
    def max_retries(self, value: int) -> None:
        self.set("max_retries", value)

    @property
    def max_qc_retries(self) -> int:
        return self._get_number("max_qc_retries", DEFAULT_MAX_QC_RETRIES, int, lo=0, hi=10)

    @max_qc_retries.setter
    def max_qc_retries(self, value: int) -> None:
        self.set("max_qc_retries", value)

    @property
    def parallel_files(self) -> int:
        return self._get_number("parallel_files", DEFAULT_PARALLEL_FILES, int, lo=1, hi=8)

    @parallel_files.setter
    def parallel_files(self, value: int) -> None:
        self.set("parallel_files", value)

    @property
    def analysis_mode(self) -> str:
        return self.get("analysis_mode", DEFAULT_ANALYSIS_MODE)

    @analysis_mode.setter
    def analysis_mode(self, value: str) -> None:
        self.set("analysis_mode", value)

    @property
    def source_lang(self) -> str:
        return self.get("source_lang", DEFAULT_SOURCE_LANG)

    @source_lang.setter
    def source_lang(self, value: str) -> None:
        self.set("source_lang", value)

    @property
    def target_lang(self) -> str:
        return self.get("target_lang", DEFAULT_TARGET_LANG)

    @target_lang.setter
    def target_lang(self, value: str) -> None:
        self.set("target_lang", value)

    @property
    def theme(self) -> str:
        return self.get("theme", "auto")

    @theme.setter
    def theme(self, value: str) -> None:
        self.set("theme", value)

    @property
    def bg_color(self) -> str:
        return self.get("bg_color", "")

    @bg_color.setter
    def bg_color(self, value: str) -> None:
        self.set("bg_color", value)

    @property
    def bg_opacity(self) -> int:
        return self._get_number("bg_opacity", 100, int, lo=0, hi=100)

    @bg_opacity.setter
    def bg_opacity(self, value: int) -> None:
        self.set("bg_opacity", value)

    @property
    def bg_image(self) -> str:
        return self.get("bg_image", "")

    @bg_image.setter
    def bg_image(self, value: str) -> None:
        self.set("bg_image", value)

    @property
    def custom_base_url(self) -> str:
        provider = self.provider
        default = DEFAULT_API_BASE_URLS.get(provider, "")
        return self.get(f"base_url_{provider}", default)

    @custom_base_url.setter
    def custom_base_url(self, value: str) -> None:
        provider = self.provider
        default = DEFAULT_API_BASE_URLS.get(provider, "")
        if value == default:
            self.set(f"base_url_{provider}", "")  # 用默认值就不存
        else:
            self.set(f"base_url_{provider}", value)

    # ---- 字幕提取（faster-whisper）----

    @property
    def whisper_model(self) -> str:
        return self.get("whisper_model", DEFAULT_WHISPER_MODEL)

    @whisper_model.setter
    def whisper_model(self, value: str) -> None:
        self.set("whisper_model", value)

    @property
    def whisper_device(self) -> str:
        return self.get("whisper_device", DEFAULT_WHISPER_DEVICE)

    @whisper_device.setter
    def whisper_device(self, value: str) -> None:
        self.set("whisper_device", value)

    @property
    def whisper_compute(self) -> str:
        return self.get("whisper_compute", DEFAULT_WHISPER_COMPUTE)

    @whisper_compute.setter
    def whisper_compute(self, value: str) -> None:
        self.set("whisper_compute", value)

    @property
    def whisper_vad(self) -> bool:
        return self.get("whisper_vad", DEFAULT_WHISPER_VAD)

    @whisper_vad.setter
    def whisper_vad(self, value: bool) -> None:
        self.set("whisper_vad", value)

    @property
    def whisper_threads(self) -> int:
        return self._get_number("whisper_threads", DEFAULT_WHISPER_THREADS, int, lo=1, hi=64)

    @whisper_threads.setter
    def whisper_threads(self, value: int) -> None:
        self.set("whisper_threads", value)

    @property
    def extract_lang(self) -> str:
        return self.get("extract_lang", DEFAULT_EXTRACT_LANG)

    @extract_lang.setter
    def extract_lang(self, value: str) -> None:
        self.set("extract_lang", value)

    @property
    def hf_mirror(self) -> str:
        return self.get("hf_mirror", "")

    @hf_mirror.setter
    def hf_mirror(self, value: str) -> None:
        self.set("hf_mirror", value)

    # ---- 在线语音识别 ----

    @property
    def asr_engine(self) -> str:
        return self.get("asr_engine", DEFAULT_ASR_ENGINE)

    @asr_engine.setter
    def asr_engine(self, value: str) -> None:
        self.set("asr_engine", value)

    @property
    def asr_service(self) -> str:
        return self.get("asr_service", DEFAULT_ASR_SERVICE)

    @asr_service.setter
    def asr_service(self, value: str) -> None:
        self.set("asr_service", value)

    @property
    def asr_model(self) -> str:
        return self.get("asr_model", DEFAULT_ASR_MODEL)

    @asr_model.setter
    def asr_model(self, value: str) -> None:
        self.set("asr_model", value)

    @property
    def asr_base_url(self) -> str:
        return self.get("asr_base_url", "")

    @asr_base_url.setter
    def asr_base_url(self, value: str) -> None:
        self.set("asr_base_url", value)

    @property
    def asr_timeout(self) -> int:
        """在线识别单请求超时（秒）：此前硬编码 600s 且不可配置——
        transcriptions 协议单块可达 44 分钟音频，慢转写会被 600s 误杀"""
        return self._get_number("asr_timeout", 600, int, lo=60, hi=3600)

    @asr_timeout.setter
    def asr_timeout(self, value: int) -> None:
        self.set("asr_timeout", value)

    def get_asr_api_key(self) -> str:
        return self.get_api_key("asr")

    def set_asr_api_key(self, key: str) -> None:
        self.set_api_key("asr", key)
