# 外围模块深度审计报告（audit_periphery）

- 审计日期：2026-09-03
- 审计范围：`core/asr_online.py`、`core/audio_splitter.py`、`core/model_download.py`、`utils/`（constants.py、token_counter.py、logger.py）、`tests/` 全部测试、`testfile/` 相关测试（覆盖面评估）、`requirements.txt`、`subtitle_translator.spec`、`main.py`
- 审计环境：Windows（Git Bash）、Python 3.14
- 严重度定义：P0=丢数据/崩溃；P1=功能错误；P2=健壮性缺陷；P3=代码质量
- 说明：本次审计仅写入本报告，未修改任何源代码。

---

## 一、问题清单

### P1-1 在线识别切块体积未计入 base64 膨胀，chat_audio 长音频必然超服务端上限

- 位置：`core/audio_splitter.py:19-20, 53, 67`；`core/asr_online.py:236-247`
- 描述：`_SIZE_SAFETY = 0.85` 的注释声称覆盖"请求体其他字段/base64 膨胀（×4/3）"，但数学上不成立：`limit_bytes = max_upload_mb × 1MB × 0.85`，`chunk_secs = limit_bytes / 8400`，编码出的原始 mp3 恰好 ≈ `limit_bytes`，经 base64 ×4/3 后为 **0.85 × 4/3 ≈ 1.133 × 上限**，超出上限 13%。以 MiMo（max_upload_mb=10，代码自述"base64 后 10MB"硬限）为例：`chunk_secs = int(10×1048576×0.85/8400) = 1061s`，单块原始 ≈8.9MB，base64 后 ≈11.9MB > 10MB。即长音频（>17.7 分钟）切出的**每个满尺寸块都会被服务端拒收**，整次提取以 RuntimeError 告终——而"一小时录播"正是该模块的设计目标场景。此外运行时护栏 `asr_online.py:238` 校验的是**原始字节数**（8.9MB < 10MB 通过），度量对象错误，拦不住该超限。
- 修复建议：安全系数按 base64 膨胀修正为 ≤0.75，且护栏改按编码后体积校验：

```python
# audio_splitter.py
_SIZE_SAFETY = 0.75  # 1/(4/3) base64 膨胀 + 请求体余量（transcriptions 直传协议可放宽到 0.9）

# asr_online.py _transcribe_chat_audio 内
b64 = base64.b64encode(raw).decode()
if len(b64) + 1024 > self.max_upload_mb * 1024 * 1024:   # 校验 base64 后体积
    raise RuntimeError(
        f"音频块编码后 {len(b64) / 1e6:.1f}MB 超过服务上限 {self.max_upload_mb}MB")
```

### P1-2 input_audio.format 硬编码 "mp3"，wav 直传时格式声明与实际数据不符

- 位置：`core/asr_online.py:243-247`
- 描述：chat_audio 协议的直传格式为 `(".mp3", ".wav")`（第 156 行），小体积 wav 会原样直传；但请求体中 `mime` 按 扩展名 正确取 `audio/wav`，`input_audio.format` 却无条件写死 `"mp3"`。OpenAI `input_audio` 规范要求 `format ∈ {"wav", "mp3"}` 且与数据一致，MiMo 兼容该协议——wav 上传可能被拒绝或按 mp3 误解码（表现：识别失败或乱文本）。测试 `testfile/test_asr_online.py:184` 有注释 `content = None  # 从 messages 校验 base64 结构`——自认未校验 payload 结构，因此该 bug 一直漏网。
- 修复建议：

```python
suffix = Path(chunk_path).suffix.lower()
mime, fmt = ("audio/wav", "wav") if suffix == ".wav" else ("audio/mpeg", "mp3")
content = [{
    "type": "input_audio",
    "input_audio": {"data": f"data:{mime};base64,{b64}", "format": fmt},
}]
```

并补一条测试：构造 wav 直传路径，断言 `messages[0]["content"][0]["input_audio"]["format"] == "wav"`。

### P2-1 API 请求零重试：瞬时网络错误/429/5xx 直接令整次提取失败

- 位置：`core/asr_online.py:126, 205-211, 250-258`
- 描述：`_make_client()` 显式 `max_retries=0`，两个协议方法又把任何异常立即包装为 `RuntimeError` 抛出，应用层无任何重试。一次网络抖动或服务端限流即失败，且此前已重编码的所有音频块算力全部作废（长音频要重传几十个块）。审计要求中的"API 失败重试"完全没有实现。
- 修复建议：按块做有界重试，仅针对瞬态异常：

```python
import time
from openai import APIConnectionError, APITimeoutError, RateLimitError, InternalServerError

_TRANSIENT = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)

def _create_with_retry(self, fn, *, attempts=3):
    for i in range(attempts):
        try:
            return fn()
        except _TRANSIENT as e:
            if i == attempts - 1:
                raise
            wait = 2 ** i * (2 if isinstance(e, RateLimitError) else 1)
            log.warning("在线识别请求失败（第 %d 次），%ds 后重试: %s", i + 1, wait, e)
            time.sleep(wait)
```

### P2-2 取消只能在块边界生效，单请求 timeout 600s 不可配置，卡死请求挂起取消最长 10 分钟

- 位置：`core/asr_online.py:94, 160-161, 202, 255`
- 描述：`cancel_event` 只在块与块之间检查；单个请求阻塞时（服务端挂起、超大块转写慢），取消最长要等 `timeout=600s` 默认值到期，且该超时既不来自设置也无参数暴露给 GUI。另一个风险：transcriptions 协议切块约 2652s（44 分钟）音频/块，600s 转写超时余量不足，正常慢转写也会被 `APITimeoutError` 判死（叠加上一条零重试即整体失败）。
- 修复建议：超时进 `OnlineASR.__init__` 并由 `from_settings` 读取设置（默认可按块时长自适应 `max(600, chunk_secs // 2)`）；取消后主动 `client.close()` 或给 SDK 传带 `httpx` 超时的独立 client，保证取消在秒级生效；每块开始/结束时都响应取消（现状已做，保留）。

### P2-3 probe 失败时用输出码率估算输入时长，产生大量空块请求（注释与代码自相矛盾）

- 位置：`core/audio_splitter.py:61-65`
- 描述：需要重编码但 `probe_duration` 失败时，回退 `duration = size / _CHUNK_BYTES_PER_SEC`——8400 B/s 是**输出** mp3 的码率，却用来估算**输入**文件时长。635MB 的 WAV（1 小时，1411kbps）会被估成约 21 小时，切出 ~71 个块，其中大部分窗口落在音频末尾之外：`_encode_chunk` 解码不到帧、产出近乎空的 mp3，逐块发往服务端得到空结果/报错，时间轴也被拉长。代码注释自己写着"用输入自身码率估算更稳"，但实现没有照做。
- 修复建议：

```python
def _input_bitrate(path: str) -> float | None:
    try:
        with av.open(path) as c:
            if c.bit_rate:
                return c.bit_rate / 8.0          # bits/s -> bytes/s
            for s in c.streams.audio:
                if s.bit_rate:
                    return s.bit_rate / 8.0
    except Exception:
        return None

duration = probe_duration(path)
if not duration or duration <= 0:
    br = _input_bitrate(path)
    duration = size / br if br else size / _CHUNK_BYTES_PER_SEC
```

### P2-4 日志目录创建失败 → 启动即静默崩溃（windowed exe 无任何可见错误）

- 位置：`utils/logger.py:11-12`；`main.py:20`
- 描述：`main()` 第一行调用 `setup_logging()`，其内部 `log_dir.mkdir(parents=True, exist_ok=True)` 与 `RotatingFileHandler` 初始化在 QApplication 之前执行且无异常保护。`%APPDATA%` 权限异常、重定向到不存在盘符、杀软锁定 `translator.log` 等场景下直接抛异常——而打包产物 `console=False`（spec 第 40 行），traceback 写进 PyInstaller 的 NullWriter，用户看到的是"双击没反应"，且没有任何日志可查（日志系统正是崩溃点）。
- 修复建议：

```python
def setup_logging(log_dir: Path | None = None) -> logging.Logger:
    logger = logging.getLogger("subtitle_translator")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        try:
            if log_dir is None:
                log_dir = Path.home() / "AppData" / "Roaming" / "SubtitleTranslator" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_dir / "translator.log", maxBytes=2 * 1024 * 1024,
                backupCount=5, encoding="utf-8", delay=True)   # delay 避免锁定即崩
            ...
            logger.addHandler(fh)
        except OSError as e:
            logger.addHandler(logging.StreamHandler())  # 降级：仅控制台
            logger.warning("日志文件初始化失败，降级为控制台日志: %s", e)
    return logger
```

### P2-5 无全局异常钩子：windowed exe 中未捕获异常静默消失

- 位置：`main.py:19-32`
- 描述：没有 `sys.excepthook`，也没有 Qt 侧的异常路由。打包后 `sys.stderr` 是 NullWriter，任何在 Qt 槽函数/worker 中漏接的异常都无影无踪：界面假死或直接退出，用户无法反馈，开发者无日志可查。对一个 `console=False` 的单文件 exe，这是最影响可维护性的缺口。对比之下 `main.py:9-12` 的 ctranslate2 预导入守卫做对了，但只防住了已知的 DLL 顺序问题。
- 修复建议：

```python
def _install_excepthook() -> None:
    log = logging.getLogger("subtitle_translator")
    def hook(t, v, tb):
        if issubclass(t, KeyboardInterrupt):
            sys.__excepthook__(t, v, tb)
            return
        log.critical("未捕获异常", exc_info=(t, v, tb))
        try:   # GUI 可用时给出可见提示
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.critical(None, "程序错误", f"发生未处理的错误，详情见日志。\n{v}")
        except Exception:
            pass
    sys.excepthook = hook
    threading.excepthook = lambda a: hook(*a[:3])
# main() 内 setup_logging() 之后调用
```

### P2-6 测试直接改写真实用户 QSettings，失败时污染用户配置

- 位置：`testfile/test_asr_online.py:233-241, 276-278, 290-341`；`testfile/test_model_download.py:276-278`（settings 流程同理）
- 描述：`test_worker_dispatch_online`、`test_settings_dialog_asr_wiring` 通过真实 `AppSettings()` 写入 `asr_engine/asr_service/asr_base_url/set_asr_api_key`（QSettings 落到用户注册表/配置文件），依赖 `finally` 恢复。一旦断言失败或进程被杀，用户的真实配置里将残留 `https://fake/v1` 与 `sk-test` Key——下次正常启动程序即指向假端点。测试与用户数据不隔离是测试卫生硬伤。
- 修复建议：在 conftest/夹具中把 `AppSettings` 的 QSettings 组织名/应用名指向临时目录（`QSettings.setPath` / `QSettings(IniFormat, temp_path)`），或 monkeypatch `AppSettings` 单例为内存桩；断言恢复放 `addCleanup` 而非裸 `finally`。

### P3-1 临时块清理：中途异常泄漏半写块与临时目录；rmdir 父目录有误删风险

- 位置：`core/asr_online.py:162-163, 177-183`；`core/audio_splitter.py:68`
- 描述：`extract()` 的 `finally` 只删除已 yield 的块并 `rmdir` 其父目录：(1) 若 `_encode_chunk` 在第 N 块中途抛错，半写文件与 `mkdtemp` 目录永远留在 `%TEMP%`（`except OSError: pass` 静默吞掉）；(2) 无条件 `Path(p).parent.rmdir()` 在未来某调用方通过 `iter_audio_chunks(..., tmp_dir=共享目录)` 复用时会误删他人目录。建议由 `iter_audio_chunks` 暴露/回收自己的目录：改为返回记录目录的上下文，或在 `extract` 中 `shutil.rmtree(out_dir, ignore_errors=True)`，并至少 `log.debug` 记录清理失败。
- 影响：磁盘垃圾缓慢累积（每块最大 ~9MB）；误删风险目前未触发（内部仅默认 tmp_dir），属防御性问题。

### P3-2 混合时间轴被整体标记为真实时间轴

- 位置：`core/asr_online.py:170-172, 192-196`
- 描述：多块识别时"任意块返回真实分段即 `estimated_timeline=False`"，但其余块可能仍是均分估算，混合产物被标记为真实时间轴，`meta` 失真，下游/用户无法据此提示"部分时间轴为估算"。建议按块记录 `got_timeline` 列表，存在混合时置 `estimated_timeline="partial"` 或在 meta 中输出逐块标记。

### P3-3 字幕时间戳可越过块/音频末尾

- 位置：`core/asr_online.py:66-72`（`split_text_evenly`）；`219-222`
- 描述：`cue_end = min(cursor + cue_dur, end_s)` 之后若 `cue_end <= cue_start` 强制 `+0.5`，且末条 `cue_dur = max(end_s - cursor, 1.0)`；当 cursor 已越过 end_s（前一条被强制延长），后续条目 end 会超过块末尾乃至音频总时长，生成时间越界的 SRT。建议对最终 `cue_end` 统一 `min(cue_end, end_s_global)`，且 `cue_dur` 下限仅在 `cursor < end_s` 时生效。

### P3-4 validate_api_key 依赖 /models 端点，部分兼容网关必然验证失败

- 位置：`core/asr_online.py:128-136`
- 描述：用 `client.models.list()` 验证 Key，但部分 OpenAI 兼容网关/聚合服务不实现 `/models`（或对 GET /models 鉴权策略不同），有效 Key 会被判为无效并提示用户检查配置——误导性失败。建议：`/models` 404 时降级为"跳过验证"，或允许用户勾选"跳过 Key 验证"。

### P3-5 _encode_chunk 错误处理缺失；frame.time 为 None 时按 0.0 静默丢帧

- 位置：`core/audio_splitter.py:81-106`
- 描述：(1) 缺 libmp3lame 编码器、容器无音轨、seek 失败等均以裸 `av.error.*` 冒泡到 GUI，用户看到的是不可读的 ffmpeg 错误串——应包装为带"音频编码失败，请检查文件是否含音轨"的 RuntimeError；(2) `t = frame.time if frame.time is not None else 0.0`：第 N 块（start>0）内首帧 time 为 None 时被当成 0.0 丢弃，后续帧若同样为 None 全部静默丢弃，造成该块内容缺失。建议 frame.time 为 None 时用 `frame.samples` 累积时长推算，或直接报错而非丢弃。

### P3-6 token_counter：编码缓存无锁；_is_cjk_char 漏全角标点与 CJK 扩展区

- 位置：`utils/token_counter.py:30-57, 76-82`
- 描述：(1) `_encoding_cache` 为普通 dict，而 `DEFAULT_PARALLEL_FILES=2` 的两个 worker 线程会并发调用 `estimate_tokens`，冷缓存下可能重复加载编码器（良性竞态，但建议 `threading.Lock` 或 `functools.lru_cache` 化）；(2) `_is_cjk_char` 不含 CJK 扩展 A/B（`\u3400-\u4DBF` 等）与全角标点（`\u3000-\u303F`、`\uFF00-\uFFEF`），朴素回退时标点密集的日文字幕被按"4 字符/token"低估，批大小偏大。属估算精度问题，量级影响小。

### P3-7 logger：非 Windows 硬编码 AppData 路径；RotatingFileHandler 未用 delay

- 位置：`utils/logger.py:11, 28-36`
- 描述：(1) 所有平台都写 `Path.home()/"AppData"/"Roaming"/...`，非 Windows 上会创建字面的 `~/AppData/Roaming` 目录，应按 `sys.platform` 或 `os.environ.get("APPDATA")` 分派；(2) `RotatingFileHandler` 默认 `delay=False` 即初始化时打开文件——与第二实例/杀软的文件锁冲突会在启动时抛异常（与 P2-4 叠加）；且 Windows 下轮转 rename 对被占用文件必然失败，建议 `delay=True` 并接受轮转失败静默（logging 自身会 handleError）。幂等的 `if logger.handlers: return` 设计是好的。

### P3-8 模型下载进度：断点续传时计数从 0 起，进度回跳/虚低

- 位置：`core/model_download.py:78-100`
- 描述：`_SilentProgressTqdm._n_local` 从 0 计数，未读取 hub 传入的 `initial`（断点续传时已存在字节数，hub 版本不同可能以 `initial=` 或首帧 `update(resume_size)` 两种方式表达）。前者会漏计已传字节：进度条回跳、结束时靠 `download_model_with_progress` 第 170-172 行强制补 `(total, total)` 掩盖。修复：`self._n_local = int(kwargs.get("initial", 0) or 0)`，并补一条"hub 以 initial=k 实例化"的测试。另：`update()` 里 `except Exception: pass` 吞掉 tqdm 内部错误过于宽泛，建议至少 `log.debug`。

### P3-9 HF 镜像在同一会话内无法回退到官方源

- 位置：`core/model_download.py:33-47, 133-134, 148-151`
- 描述：`apply_hf_mirror` 只设不撤 `HF_ENDPOINT`/`HF_HUB_DISABLE_XET`；且若 hub 在镜像调用之后才首次导入，`constants.ENDPOINT` 被固化为镜像。此后用户切回官方（`hf_mirror=""`）时 `api_kwargs={}`、`dl_kwargs` 不带 `endpoint`，仍走被固化的镜像端点。修复：`apply_hf_mirror` 接受空串时清除两个环境变量；下载官方源时显式传 `endpoint=None` 之外的官方默认值不可行（常量已固化），退而要求"切换下载源后需重启应用"并在设置页提示。

### P3-10 snapshot_dir 取最后文件父目录，仓库含子目录时返回错误路径

- 位置：`core/model_download.py:156-168`
- 描述：`last_path` 是循环中最后一个文件的路径，`snapshot_dir = Path(last_path).parent`。whisper 系仓库文件扁平所以目前正确，但自定义仓库（`_model_repo` 对未知名原样返回）若有子目录文件，最后文件的 parent 是子目录而非快照根。建议改用 `Path(last_path).parents[...]` 按快照结构定位，或下载后调用 `snapshot_download(local_files_only=True)` 拿权威路径。

### P3-11 requirements.txt：无开发依赖、无锁定；Python 3.14 轮子风险未验证

- 位置：`requirements.txt:1-8`
- 描述：(1) `pytest` 及测试依赖不在任何 requirements/lock 文件中，新环境无法按文档复现测试；(2) 全部为 `>=` 下界无上界，GUI 重依赖组合（PyQt5 5.15.x 对 Python 3.14 的官方轮子、faster-whisper→ctranslate2/av 的 3.14 轮子）未声明验证版本，存在"pip 装不上/装上崩"的风险；(3) `openai>=2.0.0` 与在线 ASR 的 `input_audio`、`extra_body` 等用法强耦合，建议锁定已验证的主版本区间。建议补 `requirements-dev.txt` 与经过验证的锁定文件（pip-tools/uv）。

### P3-12 PyInstaller spec：upx=True 对 Qt/ctranslate2 DLL 有已知损坏与误报风险；datas 相对路径脆弱

- 位置：`subtitle_translator.spec:11, 37`
- 描述：(1) `upx=True` 压缩 Qt5/ctranslate2/av 的 DLL 是 PyInstaller 社区公认的崩溃与杀软误报来源（本项目 main.py 已受 DLL 加载顺序问题困扰，更不应叠加 UPX 改写的二进制），建议 `upx=False` 或 `upx_exclude=['qwindows.dll', 'ctranslate2*.dll', 'av*.dll']`；(2) `datas=[('resources', 'resources')]` 依赖构建时工作目录解析，建议写 `datas=[(str(SPECPATH / 'resources'), 'resources')]`；hiddenimports 对 darkdetect/tiktoken_ext/ctranslate2/av 的补充是到位的。

### P3-13 测试基建：run_tests.py 不执行 tests/ pytest 套件；无 pytest 配置文件

- 位置：`testfile/run_tests.py:18-30`；项目根无 pyproject.toml/pytest.ini
- 描述：仓库存在两套互不连通的测试体系：`tests/`（pytest）与 `testfile/`（手工 assert 脚本）。`run_tests.py` 的硬编码清单只包含后者；没有 pyproject.toml/pytest.ini 指定 rootdir/testpaths，新人按任一入口跑测试都会漏掉另一半。建议：补 pyproject.toml（`[tool.pytest.ini_options] testpaths=["tests", "testfile"]`，testfile 侧用 `--collect-only` 兼容或逐步迁移），或让 run_tests.py 末尾追加 `pytest tests/`。

---

## 二、测试盲区清单

### 2.1 完全没有测试的模块

| 模块 | 缺什么用例 |
|---|---|
| `core/audio_splitter.py` | **最大盲区**。`iter_audio_chunks` 从未被直接测试（test_asr_online 仅借用 `_encode_chunk` 做 wav→mp3 工具）。缺：直传 vs 重编码决策矩阵（大小×扩展名×direct_formats）；多块切分的连续性（相邻块 start==前块 end，无空隙/重叠，覆盖到 duration 末尾）；**切块体积数学（直接命中 P1-1 的 base64 膨胀 bug）**；probe 失败回退（P2-3）；`tmp_dir` 生命周期与半写块清理（P3-1）；无音轨/坏文件错误路径；`_MIN_CHUNK_SECS` 下限钳制。 |
| `utils/token_counter.py` | 无专属测试（test_review_round2 只把它当真实依赖顺带使用）。缺：离线/加载失败回退朴素估算的正确性；失败后 None 缓存短路（不再重复网络尝试）；`estimate_tokens` lru_cache 行为；`_is_cjk_char` 边界（全角标点、韩文、扩展区）；未知模型回退 cl100k_base。 |
| `utils/logger.py` | 零测试。缺：重复调用幂等（handler 不重复）；目录不可写降级（P2-4）；`delay`/轮转配置；非 Windows 路径。 |
| `utils/constants.py` | 零测试。缺：结构契约测试——`ASR_SERVICES` 每项是 5 元组且可被 `from_settings` 位置解包（`asr_online.py:113` 强依赖元组顺序，加字段即静默错位）；`DEFAULT_PROVIDER_CONTEXT` 覆盖所有 LLM_PROVIDERS key；URL 形如 `https://...`。constants 是"数据即接口"，最便宜的一条契约测试能挡住大半配置回归。 |
| `main.py` | 零测试。缺：ctranslate2 导入守卫的两种分支（有/无 faster-whisper 环境）；excepthook 安装（修复 P2-5 后）。 |
| `gui/main_window.py`、`gui/file_panel.py`、`gui/editor_dialog.py`、`gui/preview_panel.py`、`gui/analysis_panel.py`、`gui/corpus_panel.py`、`gui/progress_widget.py`、`gui/theme_manager.py` | 完全无测试。仅 `settings_dialog` 被 testfile 两个脚本部分覆盖（引擎切换/下载流程）。GUI 可只测非渲染逻辑（信号接线、状态机、输入校验）。 |
| `services/base_llm.py`、`services/openai_service.py`、`services/claude_service.py` | 仅 test_review_fixes 零散覆盖（`chat_completion_kwargs`/reasoning 判定/ClaudeService 冒烟）。缺系统性：网络错误分类与重试、超时、流式解析、Key 校验失败路径、rate_limiter 与请求的集成。 |
| `core/translation_batch.py` | 纯数据类，仅被间接实例化；缺序列化/默认值/字段约束用例（成本低，可忽略优先级）。 |

### 2.2 现有测试的薄弱断言

| 位置 | 问题 |
|---|---|
| `tests/test_chunker.py`（全文件） | 全部用 `lambda text, model: max(1, len(text))` 替换 `estimate_tokens`，真实 token 计数与 chunker 的集成从未验证——若 MODEL_ENCODING_MAP 改坏或 tiktoken 回退逻辑出错，chunker 测试仍全绿。建议至少 1 条用真实 `estimate_tokens`（离线回退也可）的集成用例。 |
| `tests/test_corpus_manager.py:97-102` | `test_get_preset_terms_flattened_and_cached` 只断言两次调用相等+类型非空，未验证展平后的具体键值（分类嵌套是否真被展平）、也未验证"只读一次磁盘"的缓存语义（可用计数桩）。另缺：语料库 JSON 损坏/不可读时的容错路径测试。 |
| `tests/test_qc_checker.py` | 无 30% 假名残留阈值的边界用例（恰好 30%/31%）；无占位符缺失+术语未用叠加场景；`test_qc_ok_translation` 只覆盖日语→中文，中文→繁体等其他目标语言路径未测。 |
| `testfile/test_model_download.py:185-208` | `test_worker_download_progress_signal` 在测试体内**复刻**被测接线代码再断言复制品（第 196-207 行）——恒真测试，源码里接线改坏它依然绿。应改为触发真实 `run()` 路径或抽取公共函数后测源函数。 |
| `testfile/test_asr_online.py:160-188` | `test_online_asr_mimo_chat_audio` 注释自认"从 messages 校验 base64 结构"未做——P1-2（format 硬编码）因此漏网；也未测超限块被拒（P1-1 的运行时护栏）。 |
| `testfile/test_model_download.py:92-127` | `test_download_progress_aggregation` 未覆盖断点续传（hub 以 `initial=k` 重开进度条）路径（P3-8）；失败用例只断言消息含"镜像"一个字。 |
| `testfile/run_tests.py:18-30, 46` | 测试清单硬编码、90s 超时硬编码、不含 `tests/` 套件（P3-13）；脚本测试的 print 输出无机器可读结果，CI 难接入。 |
| 全部 testfile 脚本 | 断言失败时写真实 QSettings 的副作用无防护（P2-6）；除 `tests/` 外无任何 fixture/cleanup 机制，靠手工 finally。 |

### 2.3 亮点（避免误伤）

- `tests/` pytest 套件整体质量高：具体值断言（非"不抛异常"式）、monkeypatch 隔离、tmp_path 隔离文件、conftest 统一 UTF-8/offscreen。
- `test_asr_online.py` 的假客户端注入设计（`asr._make_client = lambda: fake`）与取消测试（断言"取消后不应发起请求"）是有效断言的好例子。
- `test_model_download.py` 的假 hub 注入覆盖了进度聚合的"单调不减+终值"两个关键不变量。

---

## 三、按严重度汇总

| 严重度 | 数量 | 问题编号 |
|---|---|---|
| P0（丢数据/崩溃） | **0** | — |
| P1（功能错误） | **2** | P1-1 切块体积未计 base64 膨胀（MiMo 长音频必挂）；P1-2 input_audio.format 硬编码 mp3 |
| P2（健壮性缺陷） | **6** | P2-1 零重试；P2-2 取消/超时挂起；P2-3 输出码率估输入时长；P2-4 日志初始化失败静默崩；P2-5 无全局异常钩子；P2-6 测试污染真实 QSettings |
| P3（代码质量） | **13** | P3-1 ~ P3-13（临时文件泄漏/混合时间轴/时间越界/Key 验证/编码错误处理/token 计数/日志路径/进度续传/镜像回退/快照路径/requirements/spec/测试基建） |
| **合计** | **21** | |

---

## 四、Top 3 最重要的修复

1. **P1-1**：`_SIZE_SAFETY=0.85` 覆盖不了 base64 ×4/3 膨胀，MiMo（chat_audio）满尺寸块编码后 ≈11.9MB 超过 10MB 硬限，长音频在线识别除最后一块外全部 413 失败——把安全系数降到 0.75 并把护栏改为校验 base64 后体积。
2. **P1-2**：`input_audio.format` 硬编码 `"mp3"`，wav 直传时与 `audio/wav` 数据不符，可能被服务端拒收/误解码——按文件后缀推导 format，并补 payload 结构断言（现有测试注释自认此处未校验）。
3. **P2-1**：`max_retries=0` 且应用层零重试，一次网络抖动/限流就让整次（可能几十块的）提取前功尽弃——对瞬态异常（连接/超时/429/5xx）做 2-3 次指数退避重试。
