# services 层深度审计报告（audit_services）

- 审计日期：2026-09-03
- 审计范围：`services/base_llm.py`、`services/claude_service.py`、`services/openai_service.py`、`services/prompt_builder.py`、`services/rate_limiter.py`、`config/settings.py`、`utils/constants.py`、`utils/token_counter.py`、`utils/logger.py`（全部逐行读完），并交叉核对了 `core/translation_batch.py`、`core/chunker.py`、`core/translator.py`、`core/analyzer.py`、`gui/settings_dialog.py` 的调用契约
- 审计环境：Windows（Git Bash）、Python 3.14
- 严重度定义：P0=丢数据/崩溃；P1=功能错误；P2=健壮性缺陷；P3=代码质量
- 说明：本次审计仅写入本报告，未修改任何源代码。`services/rate_limiter.py`、`utils/token_counter.py`、`utils/logger.py` 在 `docs/quality/audit_periphery.md`（P3-6、P3-7、P2-4 等）已有一轮结论，本文对这三个文件**只列新增发现，不重复既有条目**。

---

## 一、问题清单

### P1-1 输出截断/模型拒答静默降级：批次照常"翻译成功"，产出文件混入未翻译原文

- 位置：`services/claude_service.py:116-124`、`services/openai_service.py:122-123`、`services/prompt_builder.py:355-361`
- 描述：三个环节叠加成一个静默失败链：
  1. Claude 路径 `max_tokens=8192` 硬编码，但**从不检查 `response.stop_reason`**；OpenAI 路径同样不检查 `finish_reason == "length"` / `"content_filter"`。当批次较大（用户调大 `chunk_tokens`）或模型啰嗦时输出被截断，尾部条目无译文。
  2. `parse_response` 对缺失条目的处理是 `results.append(batch.entries[i].text)`（原文填充），只打 `log.warning`。
  3. 模型拒答（整段"我无法翻译…"无编号行）时所有行被丢弃，同样全部回退原文。
  最终批次无异常上抛，流水线标记翻译完成，用户拿到中日混杂的"成品"文件。QC 的 30% 假名残留阈值只对 ja→zh 有部分兜底，en→zh 完全没有。这属于"功能结果错误但应用声称成功"。
- 修复建议：在两个服务的 `call()` 内检查截断标志，并用一个"确定性错误"异常类型让 `_execute` 不重试（截断重试必然同样截断）：

```python
# services/base_llm.py
class DeterministicError(RuntimeError):
    """确定性失败（截断/解析失败等），重试必然同样失败"""

@classmethod
def _classify_error(cls, e):
    if isinstance(e, DeterministicError):
        return False, None
    ...  # 原逻辑

# claude_service.py call()
response = client.messages.create(...)
if response.stop_reason == "max_tokens":
    raise DeterministicError(
        f"批次 {batch.batch_id} 输出在 max_tokens={8192} 处截断，请减小 chunk_tokens")
return parse_response(self._extract_text(response), batch)

# openai_service.py call()
if not response.choices:
    raise DeterministicError("API 返回空 choices（可能被内容过滤）")
choice = response.choices[0]
if choice.finish_reason == "length":
    raise DeterministicError("输出被 max_tokens 截断")
response_text = choice.message.content or ""
```

  同时在 `parse_response` 中统计回退率：primary 范围内回退原文占比超过阈值（如 50%）时抛 `DeterministicError` 而不是静默填充。

### P2-1 「1 起始编号左移」启发式存在误触发路径，一旦触发整批译文错位一行

- 位置：`services/prompt_builder.py:338-353`
- 描述：输入编号从 0 开始（`build_user_message` 用 `enumerate` 默认 0），解析端用启发式把 1 起始响应整体左移。平局分支 `(hit_shift == hit_orig and len(translated) >= len(batch.entries))` 存在真实的误触发场景：**0 起始的合法响应中模型按规则 4 省略了首条**（例如首条是 `♪` 纯音效，模型不输出该行），同时末尾多吐一条越界编号（LLM 常见行为）。此时 `translated = {1..N+1}`、`min==1`、`hit_shift == hit_orig`（都少首条）、`len(translated) == len(batch.entries)` → 满足左移条件 → **所有译文整体错位一行**：第 i 条字幕拿到第 i+1 条的译文，语义串位且无任何告警。
- 修复建议：双管齐下：
  1. 规则 1 明确要求"必须为每一条输入编号输出对应行，无法翻译/纯音效也要原样输出该编号行"，消除模型省行的动机；
  2. 收紧判定：左移仅在 `hit_shift > hit_orig` 时执行；平局分支要求 `max(translated) == len(batch.entries)`（真正的 1 起始完整输出最大键必然等于条目数）且 `0 not in translated`：

```python
if (
    min(translated) == 1
    and max(translated) <= len(batch.entries)
    and (hit_shift > hit_orig or
         (hit_shift == hit_orig and max(translated) == len(batch.entries)))
):
```

### P2-2 `_execute` 把 fn() 内一切异常当"可重试"，确定性失败被整轮重试并重复计费

- 位置：`services/base_llm.py:77-80, 118`；具体触发点 `services/openai_service.py:122, 145`
- 描述：`except Exception` 覆盖了 fn() 内所有代码——包括 `response.choices[0]` 的 IndexError（部分 OpenAI 兼容网关在内容过滤/异常时返回空 `choices` 数组）、`parse_response` 内的类型错误、编程 bug 等。这些异常没有 `status_code`，`_classify_error` 走 118 行兜底返回"可重试"→ 同一确定性失败重试 `max_retries` 轮，每轮都重新 `acquire` 配额并真实计费（尤其空 choices 这种网关行为，重试 3 次结果相同，白花 3 倍钱、拖 3 倍时间）。
- 修复建议：见 P1-1 的 `DeterministicError` 方案；另外把"API 调用"与"响应解析"分开——只对网络/HTTP 层异常重试，解析错误直接抛：

```python
def call() -> list[str]:
    response = client.chat.completions.create(...)   # 可重试区
    return _parse_translation(response, batch)        # 解析移出重试区，或抛 DeterministicError
```

### P2-3 429 的 Retry-After 无上限且重试无抖动：服务端可让 worker 挂起任意久，并发重试同步惊群

- 位置：`services/base_llm.py:84, 107-114`
- 描述：(1) `float(headers.get("retry-after"))` 的结果直接作为 `time.sleep` 时长，无上限——服务端返回 `retry-after: 3600`（部分网关在配额耗尽时确实返回分钟/小时级）时 worker 线程静默挂起一小时，GUI 无任何提示也无法取消；(2) 退避公式 `backoff_base * (2 ** attempt)` 无随机抖动，`parallel_files=2` 的两个 worker 同批次触发 429/5xx 时按同一节拍同步重试，持续互相顶撞配额。
- 修复建议：

```python
MAX_RETRY_DELAY = 60.0

retry_after = min(float(headers.get("retry-after")), MAX_RETRY_DELAY)
...
delay = retry_after or backoff_base * (2 ** attempt)
delay = min(delay, MAX_RETRY_DELAY) + random.uniform(0, min(2.0, delay * 0.25))
```

### P2-4 settings 数值属性 `int()`/`float()` 裸转换无防护：单个配置键损坏即让翻译/启动崩溃；temperature 无范围校验

- 位置：`config/settings.py:296, 304, 312, 320, 328, 337, 345, 353, 401, 466`
- 描述：`int(self.get("chunk_tokens", ...))` 等十余处裸转换。代码自己已承认风险（`base_llm.py:71`、`settings.py:336` 注释"QSettings 可能存到 0"），但只给 `max_retries` 加了下限，`int()` 对**非数字垃圾值**（INI 被手工编辑、外部工具写入注册表、旧版本残留字符串）直接抛 `ValueError`——`chunk_tokens`/`temperature` 在每批翻译中都会读取，损坏后流水线每次运行必崩且难定位。另外 `temperature` 无 [0, 2] 区间校验：越界值（如 3.0）会让 OpenAI/DeepSeek 返回 400 → 走 NON_RETRYABLE 分支，整个翻译以难懂的 API 报错失败。
- 修复建议：加一个带钳制与回退的读取器，全部数值属性走它：

```python
def _get_number(self, key: str, default, cast, *, lo=None, hi=None):
    raw = self.get(key, default)
    try:
        v = cast(raw)
    except (TypeError, ValueError):
        log.warning("设置 %s=%r 非法，回退默认 %s", key, raw, default)
        v = cast(default)
    if lo is not None: v = max(lo, v)
    if hi is not None: v = min(hi, v)
    return v

@property
def temperature(self) -> float:
    return self._get_number("temperature", DEFAULT_TEMPERATURE, float, lo=0.0, hi=2.0)

@property
def chunk_tokens(self) -> int:
    return self._get_number("chunk_tokens", DEFAULT_CHUNK_TOKENS, int, lo=200, hi=100000)
```

---

### P3-1 确定性失败状态码覆盖不全：402（DeepSeek 余额不足）、413 等仍会整轮重试

- 位置：`services/base_llm.py:62, 115-117`
- 描述：`NON_RETRYABLE_STATUS = {400, 401, 403, 404, 405, 422}`。DeepSeek 余额不足返回 **402**（该项目把 DeepSeek 作为一等 provider），请求体过大返回 **413**——都是确定性失败，当前按可重试处理，白白重试并拖慢失败反馈。
- 修复建议：`NON_RETRYABLE_STATUS = frozenset({400, 401, 402, 403, 404, 405, 413, 422})`，并针对 402 给出"余额不足，请充值"的友好文案（见 P3-2）。

### P3-2 重试文案与语义不符；RuntimeError 丢弃结构化错误信息，GUI 无法区分"密钥无效/余额不足/限流"

- 位置：`services/base_llm.py:82, 91-93`
- 描述：(1) `range(max_retries)` 是**总尝试次数**，耗尽后的文案却是"已重试 3 次"（实际重试 2 次）；(2) 不可重试与耗尽两条路径都把原始异常折叠成纯文本 `RuntimeError(f"...: {e}")`，上层只能做字符串匹配。
- 修复建议：定义带 `status_code` 的 `LLMRequestError(RuntimeError)`，保留 `status`、`retry_after` 字段；文案改为"共尝试 N 次（重试 N-1 次）"。

### P3-3 重试退避与限流等待均不响应取消事件，取消延迟最长可达约 3 分钟

- 位置：`services/base_llm.py:89`（`time.sleep(delay)`）、`services/rate_limiter.py:39-64`（`acquire` 等待循环）
- 描述：`_execute` 与 `RateLimiter.acquire` 都不知道 `cancel_event`（translator 里有，未传下来）。用户点取消时，worker 可能正处于：限流等待（最长一个填充周期 ≤60s）→ 退避 sleep（≤12s）→ HTTP 请求（timeout=120s），叠加最坏约 3 分钟无响应。`core/asr_online.py` 已有 `cancel_event` 设计可参照。
- 修复建议：`_execute` 增加可选 `cancel_event` 参数，长 sleep 改为分段检查：

```python
def _sleep_cancellable(delay: float, cancel_event) -> None:
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise DeterministicError("已取消")
        time.sleep(min(0.2, deadline - time.monotonic()))
```

`RateLimiter.acquire` 的 `time.sleep(min(wait, 0.5))` 后同样插入检查点。

### P3-4 超时全部硬编码且不可配置；非流式请求断连即整次作废

- 位置：`services/claude_service.py:122 (120s), 151 (90s), 167 (15s)`、`services/openai_service.py:119 (120s), 139 (90s), 160 (15s)`
- 描述：(1) 超时不可配置，也没按模型族区分——默认模型 `gpt-5-mini` 是推理模型，大批次（`chunk_tokens` 调大后）推理+输出可能逼近甚至超过 120s，超时后原样重试再超时，3 次必然失败且每次都计费；(2) 两个服务全部使用非流式调用，120s 边缘断连时已生成的部分输出全部丢弃、整批重来（这正是审计要求中"流式响应中断处理"的现状答案：没有流式，也就没有断点续传能力，建议至少把超时做成设置项）。
- 修复建议：超时进 AppSettings（`request_timeout`，默认翻译 180s / 分析 120s），对 `is_reasoning_model(model)` 的翻译请求放大到 `max(timeout, 240)`。

### P3-5 OpenAI 翻译路径不设任何输出上限，与 Claude 的 8192 行为漂移；推理模型下 8192 预算会被推理 token 吃掉

- 位置：`services/openai_service.py:112-121`（translate 只传 temperature）、对比 `services/claude_service.py:118`
- 描述：OpenAI 路径 translate 完全没有 `max_tokens`/`max_completion_tokens`，既无截断护栏也无成本护栏；而 analyze 又设了 `min(token_limit, 8192)`。另外注意推理模型的 `max_completion_tokens` 预算**包含推理 token**：analyze 传 8192 时，复杂全文分析可能把预算全部花在推理上、返回空 `content`（`or ""` 吞掉后解析必然失败）。
- 修复建议：translate 补 `**chat_completion_kwargs(model, temperature=temperature, max_tokens=8192)` 与 Claude 对齐；analyze 对推理模型适当上调并允许 `reasoning_effort="low"`（如 SDK 支持）压缩推理占比。

### P3-6 OpenAI analyze 按固定 `token_limit` 扣限流配额，欠计量问题在 Claude 端已修复、本端未同步

- 位置：`services/openai_service.py:147-148`；对比 `services/claude_service.py:140-142`
- 描述：Claude 的 analyze 已改为 `estimate_tokens(prompt, model) + ...` 并在注释里说明"full 模式实际体积可超固定上限 2.8 倍，按固定值计费会触发更多 429"；OpenAI 端仍传固定 `token_limit`（16000）。两个行为漂移的活例子（`base_llm.py:17` 注释自述的漂移问题又添一处）。
- 修复建议：照抄 Claude 端算法：

```python
acquire_tokens = estimate_tokens(prompt, model) + estimate_tokens(
    "分析字幕内容，以 JSON 格式回复。", model)
return self._execute("全篇分析", call, self.rate_limiter, acquire_tokens, backoff_base=3.0)
```

### P3-7 `_last_base_url` 死属性；`_last_key` 以明文拼接串缓存 Key

- 位置：`services/openai_service.py:67, 92`、`services/claude_service.py:69`
- 描述：`self._last_base_url` 初始化后从未使用（openai_service），属遗留死代码；`cache_key = f"{api_key}|{base_url}"` 把明文 Key 长期驻留在实例属性中（客户端对象本身已持有 Key，此处是冗余的明文副本）。审计重点 2 的结论：**全部 9 个文件中未发现任何把 API Key 写入日志/配置文件的打印点**（`validate_api_key` 打印的 SDK 异常不含 Key，`keys.enc` 走 DPAPI/Fernet 加密，旧明文 `.keyfile` 有启动清理），明文驻留仅此一处内存副本。
- 修复建议：删除 `_last_base_url`；`_last_key` 改存 `hashlib.sha256(cache_key.encode()).hexdigest()` 或仅存 `base_url` + Key 后 4 位指纹。

### P3-8 「测试连接」模型回退跨 provider 错配：模型输入为空时用全局 provider 的模型名打到目标端点

- 位置：`services/claude_service.py:97, 130, 162`、`services/openai_service.py:106, 129, 154`；触发条件 `gui/settings_dialog.py:382-390`（`_get_clean_model_name` 可返回 `""`）
- 描述：`self._override_model or self.settings.model`——`settings.model` 按**全局** `settings.provider` 取键。当用户在设置对话框切到 provider A 测试连接、但模型下拉为空（`_get_clean_model_name()` 返回 `""`，falsy）时，`override_model=""` 回退到全局 provider B 的模型名（例如把 `gpt-5-mini` 发给 Anthropic 端点）→ 404 → 弹窗"请检查密钥和端点"，误导用户。正常翻译路径（无覆盖参数）不受影响。
- 修复建议：覆盖场景的回退应基于自身 provider：`self._override_model or self.settings.get(f"model_{self.provider_name}") or DEFAULT_MODELS.get(self.provider_name, "")`，空模型名时在 validate 前直接报"请填写模型名"。

### P3-9 `analysis_tokens > 8192` 被 Claude 路径静默钳制，设置项失真

- 位置：`services/claude_service.py:147`
- 描述：`max_tokens=min(token_limit, 8192)`——用户把 `analysis_tokens` 设为 16000（默认值正是 16000！）时实际只有 8192 生效，无任何提示；而 Claude Sonnet 系支持最高 64K 输出，钳制既无必要也未告知。
- 修复建议：放开到 `min(token_limit, 64000)`，或在设置页对超过 8192 的值给出"对 Claude 将按 8192 生效"的提示。

### P3-10 限流器 TPM/RPM 硬编码（60000/30、90000/60），不随用户账号档位调整

- 位置：`services/claude_service.py:47`、`services/openai_service.py:68`；`services/rate_limiter.py:94-100`
- 描述：两处 `get_rate_limiter(常量, 常量)`。Tier-1 以上用户实际配额远高于此——限流器会比 provider 更早进入等待，白白压低并行吞吐；反向地，免费档用户仍会打出 429（有重试兜底）。`get_rate_limiter` 按 `(tpm, rpm)` 键控与 provider 无关，未来若两个 provider 使用相同参数还会共享同一个记账桶（当前参数不同，暂未触发）。
- 修复建议：`tpm/rpm` 进 AppSettings（按 provider 分键），`get_rate_limiter` 键中加入 provider 名。

### P3-11 `prompt_overhead_tokens` 固定 +500 余量低估术语/场景注入，限流与批大小记账系统性偏低

- 位置：`services/prompt_builder.py:96-111`；配套 `core/chunker.py:163-167`
- 描述：固定开销实测了语言指南，但余量 +500 是拍脑袋：术语区最多注入 40 强制 + 40 建议 + 30 预设 = 110 条（每条 `- 日 → 中` 约 8-12 token，合计约 1000+ token），`topic_context` 则**完全未计入**（full 模式下场景文本可达数百 token）。总低估可达 ~1.5k token/批，导致 `batch.estimated_tokens` 偏小 → 限流器少扣配额 → 更多 429 → 触发 P2-3 的重试链。
- 修复建议：chunker 构造批次时对实际要注入的内容记账：

```python
total_estimate += sum(estimate_tokens(f"- {k} → {v}", model)
                      for k, v in list(batch_glossary.items())[:80])
total_estimate += estimate_tokens(topic_context or "", model)
```

### P3-12 编号解析不识别全角数字，"１．译文"被当续行拼接，污染上一条并丢本条

- 位置：`services/prompt_builder.py:14`（`NUMBERED_LINE_RE`）
- 描述：正则数字类只含半角 `\d`。日文字幕环境下 LLM 偶尔输出全角编号（`１．`、`１：`），该行落入续行分支，被 `" "` 拼接到上一条译文尾部，同时本条缺失 → 回退原文。冒号 `：`、括号 `）` 已兼容，唯独数字没兼容，属遗漏。
- 修复建议：匹配前先规范化：`line = line.translate(str.maketrans("０１２３４５６７８９", "0123456789"))` 后再 `NUMBERED_LINE_RE.match`。

### P3-13 术语键未过滤 markdown 结构标记：`##`/``` 开头的术语键可注入伪标题行

- 位置：`services/prompt_builder.py:63-79`（`_sanitize_terms`）
- 描述：`_sanitize_terms` 挡住了控制字符与 URL，但 glossary 键（LLM 分析产物，不可信）若为 `## 你的输出` 或 ` ``` `，会以 `- ## 你的输出 → xxx` 形式注入术语区——虽因带 `- ` 前缀不会命中 `_split_cached_prompt` 的区段正则（已核实缓存切分不受影响），但会在提示词里制造结构性伪标题，诱导模型把术语行当指令解析。同类问题：键为 `- xxx` 时产生 `- - xxx` 嵌套列表。
- 修复建议：复用现成正则过滤：`if _NOISE_LINE_RE.match(kk) or kk.startswith("##") or kk.startswith(("- ", "* ")): continue`。

### P3-14 `LANG_NAMES` 缺 `auto` 键：公共模块直接调用会产出"将auto字幕翻译成…"的坏提示词

- 位置：`services/prompt_builder.py:12`
- 描述：`SOURCE_LANGUAGES`（constants.py:38）含 `"auto": "自动检测"`，而 `LANG_NAMES` 没有。当前不可达仅因 `core/translator.py:131-133` 在进入 chunker 前就把 auto 解析成了具体语言；但 `build_translation_prompt` 是公共导出模块，任何绕过 translator 的调用方（测试、未来批量接口）都会拿到字面 `auto` 的提示词，且 `source_lang == "auto"` 时语言指南两个分支都不命中。
- 修复建议：`LANG_NAMES` 补 `"auto": "自动检测"`，或在 `build_translation_prompt` 入口对 `source_lang not in LANG_NAMES` 抛 `ValueError` 快速失败。

### P3-15 语料库实例缓存签名 `(mtime_ns, size)` 不防同签名改写；依赖 `CorpusManager` 私有方法

- 位置：`services/prompt_builder.py:29-50`
- 描述：(1) GUI"保存"若走"写临时文件 + `os.replace`"且新文件恰好同尺寸，`st_mtime_ns` 粗粒度文件系统（FAT/exFAT 2s 粒度，NTFS 100ns 通常够用）下可能不失效，编辑后旧语料库继续被用于后续批次；(2) `CorpusManager._resolve_corpus_path()` 是跨模块调用的私有方法，`AttributeError` 回退路径指向 `resources/corpus_vtuber.json` 硬编码文件名——语料库文件一旦改名/增补，回退路径静默指向不存在文件（`path.stat()` OSError → sig=None → 仍会 new CorpusManager，靠其内部默认值兜底，行为依赖巧合）。
- 修复建议：给 `CorpusManager` 加公开的 `corpus_path()` 类方法；签名中加入 `st_ino`（Windows NTFS 支持）或在保存时 `os.utime` 强制刷新 mtime。

### P3-16 `estimate_tokens` 的 lru_cache 以整段文本为键：全篇分析 prompt（可达数百 KB）进入 16384 条缓存

- 位置：`utils/token_counter.py:60-73`（新增发现；与 periphery P3-6 的"编码缓存无锁"不重复）
- 描述：`analyze()` 调 `estimate_tokens(prompt, model)`（claude_service.py:140），full 模式下 prompt 是整篇字幕——这样的巨型字符串会作为键在 lru_cache 里驻留（连同值的引用），16384 条的容量对短字幕条目合理、对整篇文本是内存放大；同时每次未命中都要对数百 KB 文本做完整编码+哈希。
- 修复建议：超长文本旁路缓存：

```python
def estimate_tokens(text: str, model: str = "gpt-4o") -> int:
    if len(text) > 4096:          # 长文本不缓存，避免 lru 记忆整篇字幕
        return _count(text, get_encoding(model))
    return _estimate_cached(text, model)
```

### P3-17 未收录模型一律回退 `cl100k_base`，而 gpt-5 系实际用 `o200k_base`：新模型 token 估算系统性偏低

- 位置：`utils/token_counter.py:44`（新增发现；与 periphery P3-6 的 CJK 覆盖问题不重复）
- 描述：`MODEL_ENCODING_MAP.get(model, "cl100k_base")`——`gpt-5.1`、未来的 `gpt-6`、或用户手填的 `gpt-5-mini-2025-xx`（带日期后缀的模型名不在表里）都回退 cl100k_base。o200k 对 CJK/emoji 的压缩率与 cl100k 差 10-20%，估算偏低 → 批大小偏大 → 429。gpt-5 系已入表，但**前缀变体**没覆盖。
- 修复建议：未知模型先做前缀归一：`if model.startswith(("gpt-5", "o1", "o3", "o4")): encoding_name = "o200k_base"`。

### P3-18 logger 无敏感信息脱敏过滤器：SDK 异常全文进入 DEBUG 级 2MB×5 常驻磁盘日志，缺纵深防御

- 位置：`utils/logger.py:8-44`（新增发现；与 periphery P2-4/P3-7 的初始化崩溃/路径问题不重复）
- 描述：当前代码核实无 Key 打印点（见 P3-7 结论），但 `validate_api_key`（claude_service.py:171、openai_service.py:165）与 `_execute` 的 `%s` 异常打印把 SDK 异常对象全文交给 formatter——SDK 异常含请求 URL、响应体；一旦未来有人打印 headers/请求体调试，或用户使用"Key 放在 URL 查询参数里"的网关，敏感串就会进入长期落盘的日志。日志系统作为最后一道防线没有脱敏机制。
- 修复建议：加全局 Filter 对常见密钥形态掩码：

```python
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+|api[-_]?key=[^\s&]+)", re.I)

class RedactFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        red = _SECRET_RE.sub("[REDACTED]", msg)
        if red != msg:
            record.msg, record.args = red, None
        return True

logger.addFilter(RedactFilter())
```

### P3-19 Windows 上密钥解密失败时会无谓创建 `fernet.key`；密钥文件写入无 fsync

- 位置：`config/settings.py:220-231`（`_load_keys` 的 Fernet 回退）、`163-168`（`_get_legacy_fernet` 生成路径）、`246-251`（`_save_keys`）
- 描述：(1) `_load_keys` 在 DPAPI 解密失败后无条件走 `_get_legacy_fernet()`——该方法在**任何平台**都会生成并落盘 32 字节随机密钥，Windows 上（本应用主目标平台）这是一个无意义的副作用文件 `%APPDATA%/SubtitleTranslator/fernet.key`；(2) `tmp.write_bytes(...)` + `os.replace(...)` 之间无 `os.fsync`，掉电/强杀可能让 rename 后的 `keys.enc` 是零长或半写文件 → 已保存的全部 Key 丢失（DPAPI 无法恢复），用户只能重输。
- 修复建议：Fernet 回退分支加 `if os.name != "nt"` 门卫；写入后 fsync：

```python
with open(tmp, "wb") as f:
    f.write(encrypted)
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, self._keys_file)
```

### P3-20 解密后的 JSON 不做 dict 类型校验，坏数据延迟到 `get_api_key` 才 AttributeError

- 位置：`config/settings.py:212, 223, 229, 235`
- 描述：`self._keys = json.loads(decrypted)` 三处赋值都不校验类型。若 `keys.enc` 解密成功但内容是 list/str（旧版本 bug 产物、手工构造），`get_api_key` 调 `self._keys.get(...)` 时才在任意调用点抛 AttributeError——距离根因（损坏的密钥文件）很远，难排查。
- 修复建议：`loaded = json.loads(decrypted); if not isinstance(loaded, dict): raise ValueError("keys.enc 结构非法")`，让异常落入既有的"解密失败告警 + 保留原文件"分支（234 行）。

### P3-21 非 Windows 回退路径硬编码 `~/AppData/Roaming`；DPAPI 注释把标志 0 说成 CRYPTPROTECT_UI_FORBIDDEN

- 位置：`config/settings.py:49, 100`
- 描述：(1) `_get_data_dir` 的 APPDATA 兜底是 `Path.home() / "AppData" / "Roaming"`——非 Windows 上会创建字面的 `AppData/Roaming` 目录（与 periphery P3-7 对 logger 的批评同病，但这是另一个文件，单列）；(2) `CryptProtectData(..., 0, ...)` 的注释写"标志 0：CRYPTPROTECT_UI_FORBIDDEN"——实际 `CRYPTPROTECT_UI_FORBIDDEN = 0x1`，传 0 是"无标志"。行为（0）在服务上下文可接受，注释错误会误导后续维护者。
- 修复建议：路径按 `sys.platform` 分派（非 Windows 用 `~/.config/SubtitleTranslator`，与 QSettings IniFormat 惯例一致）；注释改为"标志 0：无特殊标志（当前用户作用域为 DPAPI 默认行为）"。

### P3-22 `AppSettings` 单例 `__new__` 非线程安全

- 位置：`config/settings.py:177-186`
- 描述：`if cls._instance is None: cls._instance = super().__new__(cls)` 无锁。首次并发构造（主线程初始化 GUI 与 worker 线程首取设置几乎同时）时可能创建两个实例：败者的 `_keys` 字典与胜者不同，出现"一边有 Key 一边没有"的诡异状态。当前时序上主线程几乎总是先行（概率低），但 `parallel_files=2` 的 worker 与设置对话框写回并发时并非零风险。
- 修复建议：

```python
_instance_lock = threading.Lock()

def __new__(cls):
    with cls._instance_lock:
        if cls._instance is None:
            inst = super().__new__(cls)
            inst._initialized = False
            cls._instance = inst
    return cls._instance
```

### P3-23 `DEFAULT_MODELS["custom"]=""` 服务层无防御：空模型名直达 API 400；openai 兜底上下文 400k 偏宽

- 位置：`utils/constants.py:93, 82-87`
- 描述：(1) custom provider 默认模型为空串，`OpenAIService.translate_batch` 的 `model` 会以 `""` 直达请求体 → 400（NON_RETRYABLE），报错文案是服务端原文，用户难懂——依赖 GUI"custom 必填模型"的约束，服务层无兜底校验；(2) `DEFAULT_PROVIDER_CONTEXT["openai"]=400000` 对未知 OpenAI 模型过于宽松（gpt-4o 系只有 128k），未知模型名时上下文裁剪按 400k 计算，可能组装出超限请求。
- 修复建议：`translate_batch`/`analyze` 入口对空模型名抛带指引的 `ValueError("未配置模型名，请在设置中选择或输入")`；openai 兜底降到 128000。

---

## 二、亮点（避免误伤）

- `_execute` 统一重试执行器方向正确：限流预扣-失败返还的记账闭环、NON_RETRYABLE 直抛省时间、SDK 双层重试禁用避免相乘，都是对的设计（问题在于异常分流粒度，见 P2-2）。
- 密钥存储整体合格：DPAPI（当前用户域）+ 原子写 + 0600 + 旧明文 `.keyfile` 启动清理 + 旧 Fernet 格式迁移保留原文件，"测试连接"不再触碰全局设置。全部文件验证无 API Key 日志泄漏点。
- Prompt Caching 的"恒定前缀/批级可变"两块拆分（claude_service.py:103-113）有真实的省钱推理，且 `_split_cached_prompt` 与 `_BATCH_VARIABLE_SECTION_RE` 的匹配边界（`### ` 不误命中）核对无误。
- `_sanitize_terms`/`_has_control_or_url` 对不可信 glossary 的注入防御、`topic_context` 的 `"""` 围栏剥离 + "按数据对待"声明，是提示词注入面的正确第一层。
- `parse_response` 的小数前缀保护（`3.5 亿` 不当编号行）与噪声行过滤考虑细致。

---

## 三、按严重度汇总

| 严重度 | 数量 | 问题编号 |
|---|---|---|
| P0（丢数据/崩溃） | **0** | — |
| P1（功能错误） | **1** | P1-1 截断/拒答静默降级，产出混入未翻译原文 |
| P2（健壮性缺陷） | **4** | P2-1 编号左移启发式误触发整批错位；P2-2 确定性异常被整轮重试重复计费；P2-3 Retry-After 无上限且无抖动；P2-4 settings 数值裸转换崩溃 + temperature 无范围校验 |
| P3（代码质量） | **23** | P3-1 ~ P3-23 |
| **合计** | **28** | |

P3 索引：P3-1 确定性状态码不全（402/413）；P3-2 重试文案/结构化信息丢失；P3-3 退避与限流等待不响应取消；P3-4 超时硬编码/非流式；P3-5 OpenAI 无输出上限；P3-6 OpenAI analyze 欠计量；P3-7 死属性/明文 Key 内存副本；P3-8 测试连接模型跨 provider 错配；P3-9 analysis_tokens 静默钳制；P3-10 限流参数硬编码；P3-11 提示词开销低估；P3-12 全角数字编号；P3-13 术语键结构标记；P3-14 LANG_NAMES 缺 auto；P3-15 语料库缓存签名；P3-16 lru 缓存内存放大；P3-17 未知模型编码回退偏差；P3-18 日志无脱敏过滤器；P3-19 Windows 无谓 fernet.key/无 fsync；P3-20 keys JSON 类型校验；P3-21 非 Windows 路径/DPAPI 注释；P3-22 单例线程安全；P3-23 custom 空模型名/上下文兜底偏宽。

---

## 四、Top 5 最重要的修复

1. **P1-1**：输出截断（Claude `stop_reason`/OpenAI `finish_reason` 均未检查）与模型拒答会让批次静默回退原文并标记翻译成功——先加截断检测与 `DeterministicError`，再把高回退率变成显式失败。
2. **P2-2**：`_execute` 的 `except Exception` 把解析错误、空 `choices` 等确定性失败当可重试，每轮真实计费——引入 `DeterministicError` 并把解析移出重试区。
3. **P2-1**：1 起始编号左移启发式的平局分支可被"合法省略首条 + 越界尾行"误触发，整批译文错位一行——在规则 1 强制输出全部编号行，并收紧平局判定条件。
4. **P2-4**：settings 十余处 `int()/float()` 裸转换，任一配置键损坏即让每次翻译必崩，temperature 越界还会触发不可重试的 400——统一走带钳制与回退的 `_get_number`。
5. **P2-3**：429 的 Retry-After 直接进 `time.sleep` 无上限（服务端可让 worker 挂起小时级）且重试无抖动——封顶 60s 并加随机抖动，顺带让退避可响应取消（P3-3）。
