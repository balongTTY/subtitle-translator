# core / I/O 层深度审计报告

- 审计对象：`core/chunker.py`、`core/merger.py`、`core/subtitle_io.py`、`core/extractor.py`、`core/tag_handler.py`、`core/qc_checker.py`、`core/corpus_manager.py`
- 交叉验证参考：`core/translator.py`、`core/translation_batch.py`、`services/prompt_builder.py`、`utils/token_counter.py`、pysubs2 1.8.1 实测行为
- 严重度定义：P0=丢数据/崩溃，P1=功能错误，P2=健壮性缺陷，P3=代码质量
- 日期：2026-09-03

---

## 一、chunker.py（字幕批次切分）

### 1. [P2] chunker.py:88-96 — 单条超长字幕只告警不拆分，整批必然失败回退原文
单条字幕 token 超过 `max_tokens` 时仅 `log.warning` 后继续，让它独占一批。此时该批 = 超长条目 + 最多 5 条 overlap + 约 2000+ token 提示词固定开销（`prompt_overhead_tokens`），几乎必然超过模型上下文，LLM 调用失败 → 重试 `max_qc_retries` 次全失败 → 整批回退原文标记 needs_review。字幕文件里偶发的超长行（如整段歌词被合并成一条、解析器把 malformed 块拼进前一条——pysubs2 实测会这样）会触发此路径，用户得到整块未翻译文本。

**修复建议**：对超长条目按句读边界（`。！？\n` 等）强制切成多条临时子条目再分批；或至少把该条目从批次预算中扣除并隔离到独立批次，失败后按句回退而不是整条回退：

```python
if current_token_estimate > config.max_tokens:
    pieces = split_long_text(entries[start_idx].text, config.max_tokens)
    if len(pieces) > 1:
        log.warning("条目 #%d 超长，已强制拆分为 %d 段", entries[start_idx].index, len(pieces))
        # 用拆分后的子条目替换 entries[start_idx] 参与分批，合并时按 index 顺序拼回
```

### 2. [P2] chunker.py:118 — 场景断点回看窗口永远排除 j=0（首条之后的大间隔无法成为断点）
`range(len(batch) - 1, max(0, len(batch) - look_back), -1)` 的 stop 是开区间，`j=0` 从不被评估。当本批唯一 ≥`break_on_gap_ms` 的时间间隔位于第 0、1 条之间时找不到断点，只能在当前位置硬切，场景切换处的批次边界质量下降。

**修复建议**：

```python
for j in range(len(batch) - 1, max(-1, len(batch) - look_back), -1):
    # j=0 时断点后 batch 仍保留 1 条（batch[:1]），current_idx = start_idx + 1，合法
```

### 3. [P2] chunker.py:198-199 — 话题索引 fallback 把原始索引直接当过滤后位置使用
`pos = index_to_pos.get(start, start)`：当 LLM 给出的 `start_idx` 指向被跳过的条目（绘图/音效）或越界值时，拿不到映射就把**原始索引**直接当**过滤后位置**用。跳过条目越多，话题锚点偏移越大（`translatable[pos]` 会指向更靠后的条目），`_get_topic_for_range` 注入的话题上下文张冠李戴。

**修复建议**：fallback 时用 bisect 找 ≤start 的最近有效位置，找不到就丢弃该段：

```python
pos = index_to_pos.get(start)
if pos is None:
    lower = [p for i2, p in index_to_pos_sorted if i2 <= start]  # 预排序后 bisect
    if not lower:
        continue
    pos = lower[-1]
index[pos] = topic
```

### 4. [P3] chunker.py:106 — 单批硬上限预算未扣除提示词固定开销
`max_tokens` 只与条目 token 比较，`prompt_overhead_tokens(source_lang)`（实测约 2000+，见 prompt_builder.py:96-111 注释）只在 `total_estimate` 记账时才加上。`max_tokens = min(3000, ctx//2)` 的"留一半余量"设计与 overhead 之间的关系靠巧合而非约束：当 `ctx//2` 接近 3000 且术语表/场景块较长时，实际请求仍可能逼近上下文上限。

**修复建议**：`budget = max(500, config.max_tokens - prompt_overhead_tokens(source_lang))`，循环内用 `budget` 替代 `config.max_tokens` 判断。

### 5. [P3] chunker.py:39-44 — min_entries / max_entries 未接入设置
`_get_config()` 不传 `min_entries`/`max_entries`，永远是 dataclass 默认 3/30，与其它可配置项（chunk_tokens、overlap_entries）不一致，用户无法调整短句字幕的批次条数上限。

### 6. [P3] chunker.py:38 — context_limit 配置过小时 target > max_tokens
`max(ctx // 2, 500)` 下限 500，而 `target_tokens` 来自用户设置（默认 2000）。若用户把 context_limit 设小，每批都会立即触发硬上限，批次碎成单条——行为正确但与"目标 token"设置明显不符。建议对 `chunk_tokens` 一并 clamp：`target = min(target, budget)`。

### 7. [P3] chunker.py:193-200 — 同一位置的多个话题段相互静默覆盖
`index[pos] = topic` 后段覆盖前段，无告警。LLM 返回重复 `start_idx` 时用户无法察觉话题信息丢失。建议冲突时 log.debug 或拼接。

### 已验证无问题的边界（chunker/merger）
- 空输入：`chunk([], ...)` 在 L57-58 提前返回，安全。
- 死循环：所有分支 `current_idx` 相对 `start_idx` 至少 +1（硬上限分支最小 `start_idx+1`），`while` 必然推进。
- 索引越界：`entries[start_idx + j + 1]` 上界即当前候选 i < len(entries)（L119 注释正确）；gap 断点截断后 `current_idx = start_idx + j + 1` 与保留条目数一致。
- overlap 编号错位：`build_user_message` 对含 overlap 的 `batch.entries` 从 0 连续编号，`parse_response` 按 `primary_start_idx/primary_end_idx` 截取，再与 `primary_entries` 按位置配对——三处一致，**无错位**；1 起始编号的左移对齐启发（prompt_builder.py:338-353）逻辑自洽。
- 占位符前缀碰撞：`"<TAG_10>".replace("<TAG_1>", …)` 因闭合 `>` 不匹配，无前缀误替换。

---

## 二、merger.py（翻译结果合并）

### 8. [P1] merger.py:34-43 — 空译文不触发任何回退，输出空行且丢失 ASS 标签
`result.translated_text` 为空串时直接参与 `restore_tags`（空串不含 `<TAG_`，L47 检测不触发），`final_text = ""`。QC 的 `is_batch_passable` 虽拦截"空译文"，但**重试耗尽路径**（translator.py:330-334）会把带"译文为空"的 results 原样并入 `all_results`；随后 `save()` 的 `entry.text or ssa.events[i].plaintext` 兜底成**去掉全部标签的原文 plaintext**——ASS 行的样式标签丢失，且这一降级只存在于日志。

**修复建议**（merger 内统一兜底，保住带标签原文）：

```python
if not translated_text or not translated_text.strip():
    translated_text = entry.original_text  # 保标签的原文，而非空串
    result = all_results.get(entry.index)
    if result is not None:
        result.status = "needs_review"
        if "译文为空，已回退原文" not in (result.qc_issues or []):
            result.qc_issues = list(result.qc_issues or []) + ["译文为空，已回退原文"]
```

### 9. [P2] merger.py:47 — `"<TAG_"` 字面检测存在漏报与误报
- 漏报：LLM 把占位符改写成 `<tag_0>`（小写）或 `< TAG_0>` 时，`restore_tags` 不命中、检测也不命中（大小写敏感），字面 `<tag_0>` 直接进成品。QC 的 `<TAG_\d+>` 同样大小写敏感，两头都漏。
- 误报：源字幕本身含字面 `<TAG_0>`（讲 HTML 的课程/编程字幕）时，`extract_tags` 不会提取它（不在 `{}` 中、标签名不匹配白名单），LLM 原样带回 → 被误判"丢标签"整条回退原文。

**修复建议**：与 QC 共用同一宽容正则，并对不在 tag_map 中的占位符显式告警：

```python
stray = re.findall(r"<\s*/?\s*TAG_\s*\d+\s*>", final_text, re.IGNORECASE)
if stray:
    final_text = entry.original_text
    ...  # 标记 needs_review
```

### 10. [P2] merger.py:49-55 — merge 直接改写调用方传入的 TranslationResult 对象
副作用（`status`/`qc_issues` 变更）未在 docstring 声明；同一 result 对象被 UI 统计、质检汇总共享时产生跨阶段状态污染（如 QC 统计的通过数在 merge 后悄悄变化）。建议 merge 内对需回退的 result 做浅拷贝后再改，或在 docstring 明示"会原地修改"。

### 11. [P3] merger.py:42 — 每条重复跑两遍标签正则
translator.py:140-142 已对每条提取过一次 tag_map，merger 再对 `original_text` 全量重提（万条文件 = 2 万次正则）。建议 translator 提取后把 tag_map 缓存到 entry（或模块级 LRU），merger 优先取缓存。

### 12. [P3] merger.py:33-38 — all_results 中不属于任何条目的索引被静默丢弃
健壮性考虑应统计并 `log.warning`（索引漂移是上游 bug 的信号，静默丢弃会掩盖问题）。

---

## 三、subtitle_io.py（SRT/ASS/VTT 解析与写出）

### 13. [P2] subtitle_io.py:80,87,93,97 — 中间解码结果的 pysubs2 解析异常会中断整个编码回退链
每个编码分支里的 `pysubs2.SSAFile.from_string(text)` 均无 try 保护。当某个"错误编码恰好能解码"的中间结果解析失败（典型：UTF-16LE 文件被 shift_jis 解出内嵌 NUL 的文本 → `FormatAutodetectionError`），异常直接冒到 `_load_any_encoding` 变成"无法识别字幕文件格式（文件可能为空或已损坏）"，**GB18030 / latin-1 兜底永远执行不到**——回退链被自己设计要抵御的那类失败击穿。

**修复建议**：每个分支的解析调用单独包裹：

```python
try:
    text = raw.decode("shift_jis")
except UnicodeDecodeError:
    pass
else:
    try:
        ssa = pysubs2.SSAFile.from_string(text)
    except pysubs2.Pysubs2Error:
        pass  # 该编码下解出的文本无法按字幕解析 → 继续尝试下一编码
    else:
        if _KANA_RE.search("\n".join(e.plaintext for e in ssa.events)):
            return ssa
```

### 14. [P1] subtitle_io.py:62-103 — UTF-16（含 BOM）完全不在回退链，此类文件必然加载失败
Windows 记事本"Unicode"导出的 SRT/ASS 是现实常见的输入。UTF-16LE 字节流：utf-8 解码失败 → shift_jis 能解出含 NUL 的文本但 pysubs2 无法识别格式（实测 NUL 打断时间戳正则）→ 由问题 13 中断或最终 latin-1 出 NUL 乱码解析失败 → 用户得到误导性的"文件可能为空或已损坏"。

**修复建议**：链首加 BOM 探测：

```python
raw = Path(filepath).read_bytes()
if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
    try:
        return pysubs2.SSAFile.from_string(raw.decode("utf-16"))
    except (UnicodeDecodeError, pysubs2.Pysubs2Error) as e:
        log.warning("UTF-16 解析失败: %s", e)  # 继续走通用回退链
```

### 15. [P2] subtitle_io.py:85-89 — GB18030 先于"无假名 Shift-JIS 重试"，纯汉字日文字幕会被静默解成 GBK 乱码
假名判定 `_KANA_RE` 只认 `ぁ-ん/ァ-ヶ`；全汉字/罗马字日文字幕（歌词、标题屏显、纯汉字台词）在步骤 2 因无假名落空后，GB18030 对任意 SJIS 字节串几乎总能"成功"解码出汉字乱码并直接返回——**静默乱码**，无任何告警。（半角片假名 U+FF61-FF9F 不在 `_KANA_RE` 范围内，已验证 GBK 中文不会因此误判，该设计本身是对的；问题仅出在无假名日文。）

**修复建议**：GB18030 成功后加合理性校验，不通过则尝试 SJIS 重试并择优：

```python
text = raw.decode("gb18030")
ssa = pysubs2.SSAFile.from_string(text)
if _KANA_RE.search(...) or _looks_like_cjk(text):   # 常用汉字/词频启发
    return ssa
# 否则尝试 shift_jis 重试（纯汉字日文），比较两者假名/高频字命中后择优
```

### 16. [P2] subtitle_io.py:87 — Big5（繁体中文）不在链内且会被 GB18030 静默吞掉
GB18030 极少抛 `UnicodeDecodeError`，Big5 文件几乎总会被 GB18030 解成乱码直接返回。建议同问题 15：解码成功后做常用字合理性校验，失败则尝试 `big5` 并按命中率择优。

### 17. [P2] subtitle_io.py:25-34, 120-127 — load/save 按位置配对，依赖"entries 与 events 顺序一一对应"的隐含约定
load 赋 `index=i` 且当前 translator 不过滤 entries，流水线内安全；但 `save()` 是公共 API（extractor.py:383 也在用），一旦传入过滤/重排过的 entries：事件 i 被错误条目覆盖（**文本与时间轴错位**）、尾部多余 events 残留未翻译原文、被过滤条目在输出中消失或重复。

**修复建议**：save 前置校验并按 index 对齐：

```python
if len(entries) != len(ssa.events):
    log.error("条目数(%d)与事件数(%d)不一致，拒绝按位置覆盖", len(entries), len(ssa.events))
    raise ValueError("entries 与原文件事件数不一致，无法安全保存")
```

### 18. [P2] subtitle_io.py:122, 130 — 空文本静默回退无标签 plaintext 且无逐条记录
`entry.text or ssa.events[i].plaintext`：空译文被换成"去标签原文"，ASS 样式标签丢失、未翻译事实只留在日志里。建议优先回退 `original_text`（保标签）并逐条 `log.warning` 生成可审计清单。

### 19. [P2] subtitle_io.py:25-34 — ASS Comment 行被当作可翻译条目送入流水线
实测 pysubs2 把 `Comment:` 行放入 `ssa.events` 且 `is_comment=True`。load 不区分 → 制作注释被送 LLM 翻译（浪费 token、污染 QC 统计），save 时文本已被改写但仍写回 Comment 行。建议 load 时跳过或原样透传：

```python
for i, event in enumerate(ssa.events):
    if event.is_comment:
        continue  # 注释事件不进入翻译流水线
```

### 20. [P3] subtitle_io.py:141 — ssa.save 非原子写
直接写目标路径，进程中途被杀留下半截文件。translator/extractor 侧有 .bak 兜底，但"新文件写一半"本身可能被 UI 当成功。建议与 corpus_manager 一致：tmp + `os.replace`。

### 21. [P3] subtitle_io.py:144-150 — `_fix_srt_bold` 只处理独立 `{\b1}`/`{\b0}` 块
复合块如 `{\b1\i1}` 中的 `\b` 在 SRT 导出时仍被 pysubs2 静默丢弃（docstring 已知限制但未覆盖此形态）。建议用 `re.sub(r"\{[^}]*\\b1[^}]*\}", lambda m: "<b>" + 剥b后的其余标签 + "</b>"样式, ...)` 或至少在 docstring 中列明。

### 22. [P3] subtitle_io.py:68-72 — utf-8-sig 与 utf-8 重复尝试
`utf-8-sig` 是 utf-8 的超集（BOM 可选），第二个编码永远不会在第一个失败时成功。无害冗余，可删。

### 23. [P3] subtitle_io.py:32 — `hasattr(event, "style")` 恒为真
pysubs2 `SSAEvent` 恒有 style 属性；若为兼容测试假对象应加注释说明。

### 已验证无问题的边界（subtitle_io）
- CRLF/LF、BOM（utf-8-sig）、编号不连续（pysubs2 忽略编号按事件解析、保存时重排）、逗号/点号毫秒（两种都支持）、ASS `H:MM:SS.CC`（原生支持）——均正常。
- malformed 块：pysubs2 会把无法识别的块**拼进前一条事件的文本**（实测 "bad block" 变成前条的 `\N` 续文）。这不是本层代码 bug，但它与问题 1（超长条目）联动：被拼接的条目可能变得极长。建议在 load 后对异常长的条目（如 >500 字符）log.warning 提示源文件可能损坏。
- VTT 的 NOTE/STYLE 块：pysubs2 正确跳过（实测）；cue 设置（position:50%）正常解析保留。
- pysubs2 解析 SRT 时会把 `<i>/<b>/<u>/<font>` 转成 `{\i1}` 等 ASS 标签，随后走占位符链路保留——SRT 内联标签**不会**在翻译中丢失（此前的怀疑经实测排除）。

---

## 四、extractor.py（视频字幕提取）

### 24. [P2] extractor.py:196-244 — 模型下载/构造在类级锁内执行，第二个实例表现为无限期假死
`_get_model` 持 `SubtitleExtractor._lock` 期间执行可能数 GB 的下载 + `WhisperModel` 构造；另一线程在锁上无超时阻塞，GUI 无任何提示（用户以为程序挂了）。

**修复建议**：锁内只做缓存检查与"loading 中"标记，下载/构造放锁外；或 per-key 的 `concurrent.futures.Future` 缓存，等待方带超时轮询并向 UI 发"等待另一任务加载模型"提示。

### 25. [P2] extractor.py:266-272 — CPU 回退重试永久改写 `self.device`，且降级状态不可见
`self.device = "cpu"` 后同实例后续提取全部静默走 CPU；GPU 不可用这一事实只在 log 里。建议记录 `self.degraded = "cpu"` 并通过信号/回调通知 UI 供展示。

### 26. [P2] extractor.py:135-143 — 备份失败后 `out.replace` 抛异常，已完成的识别结果整体丢弃
`backup.unlink()` 失败被吞（OSError pass），随后 `out.replace(backup)` 若因目标被占用（播放器/杀软/同步盘锁定 .bak）抛出 → **数十分钟的 GPU 推理结果不落盘、无缓存**，只能整个重跑。

**修复建议**：replace 失败时退化为换名保存而不是放弃：

```python
try:
    out.replace(backup)
except OSError as e:
    log.warning("备份失败(%s)，改用新文件名保存", e)
    n = 1
    while (p := out.with_name(f"{out.stem}_{n}.srt")).exists():
        n += 1
    return str(p)
```

### 27. [P2] extractor.py:330-350 — run() 中 per-file 循环之前的异常会让 `all_completed` 永不发射
`SubtitleExtractor(model_size="large-v3-turbo")` 兜底构造（L350）不在任何 try 内；一旦抛异常，run() 直接跳出 → UI 永远停在"提取中"（translator.py 同型问题：L93 `AppSettings()` 在 try 外）。建议整个 run 体外层再包一层：

```python
try:
    ...  # 现有逻辑
except Exception:
    log.exception("提取工作线程异常退出")
    self.signals.all_completed.emit({fp: "" for fp in self.filepaths})
finally:
    QThread.currentThread().quit()
```

### 28. [P3] extractor.py:292 — `duration` 为 None 时 `int(None * 1000)` 抛 TypeError
faster-whisper 常规路径不会，但对第三方/测试注入的 info 对象缺防御：`int((getattr(info, "duration", 0) or 0) * 1000)`。

### 29. [P3] extractor.py:445-463 — cancel() 在主线程 `thread.wait(3000)`，最多卡 UI 3 秒
识别中的单次 transcribe 不可中断属已知限制，但 UI 层面应有"正在取消"状态；另 `self._cancelled`（L410 赋值、L446 赋值）**从未被读取**，是死代码。

### 30. [P3] extractor.py:35-49 — `_live_threads` 依赖 finished 信号回收，线程卡死则条目泄漏
进程生命周期内的有界泄漏，docstring 已知，可接受；建议在应用退出钩子里对未结束线程 `terminate()` 兜底。

### 关于"子进程调用、ffmpeg 依赖失败处理"（用户指定审计点）
本文件**无 subprocess 调用**：音频解码走 PyAV（自带 ffmpeg 动态库），不存在"系统未安装 ffmpeg"这一失败形态；库缺失/损坏会以 ImportError/RuntimeError 在 `_get_model`/`transcribe` 抛出 → run() 的 per-file except → `extraction_failed` 信号，处理路径完整。`check_whisper_available` 用 `find_spec` 预检避免触发 HF import 副作用，设计正确。**无问题**。

---

## 五、tag_handler.py（标签处理）

### 31. [P2] tag_handler.py:12 — SRT 标签白名单缺 `<ruby>/<rt>/<v>/<c>/<s>` 等，白名单外标签裸奔进提示词
`<v 主播名>`、`<c.colorE5E5E5>`（VTT 常见）、`<ruby>`（日文注音）不在白名单：不会被占位符保护，原样进入提示词被 LLM 当正文改写/删除，QC 也不检查它们。pysubs2 解析时会剥掉一部分（实测 `<v>/<c>` 被剥、`<font>` 颜色信息被丢），但该行为非契约，且手工构造的 entry 绕过解析。

**修复建议**：白名单扩为 `i|b|u|s|font|ruby|rt|rp|v|c`；或改为"全部 `</?[a-zA-Z][^>]*>` 占位"，配合对源文本中数学比较（`a < b > c`）的预判降级。

### 32. [P2] tag_handler.py:36-39 — 源文本中字面 `<TAG_N>` 与占位符体系冲突，无任何防护
源字幕本身含 `<TAG_0>`（讲 HTML 的课程字幕）时：`extract_tags` 不替换它（不在 `{}` 中、标签名不匹配）→ 原样进提示词 → LLM 原样带回 → merger.py:47 误判"占位符未还原"**整条回退原文**并标记 needs_review（正常翻译结果被丢弃）。

**修复建议**：`extract_tags` 开头先把源中已有形如 `<TAG_\d+>` 的字面量也编入 tag_map（当作普通标签处理），使占位符体系对源文本封闭：

```python
LITERAL_TAG_RE = re.compile(r"<TAG_\d+>", re.IGNORECASE)
cleaned = LITERAL_TAG_RE.sub(replacer, text)   # 先于 ASS/SRT 提取
```

### 33. [P3] tag_handler.py:7 — `ASS_TAG_PATTERN` 无法处理字面 `\{`/`\}`
ASS 格式本身无转义机制，属固有限制；建议 docstring 注明，避免后人误修。

### 34. [P3] tag_handler.py:53-55 — `has_drawing` docstring 与实现不一致
docstring 说检测 `{\p1}...{\p0}`，实现只认 `\p1-\p9`（`\p0` 是绘图结束标记，不匹配是**正确**的），应更正 docstring 防误导。

### 35. [P3] tag_handler.py:57-71 — `is_skip_entry` 不跳过仅含 `\N`/`\h` 的行
ASS 空行分隔条目剥标签后剩字面 `\N`，既非空也不匹配 MUSIC_ONLY_RE → 被送去翻译。建议剥标签后 `replace("\\N", "").replace("\\h", "")` 再判空。

---

## 六、qc_checker.py（质检）

### 36. [P2] qc_checker.py:78-85 — 占位符校验用 set 比较，不校验出现次数
LLM 输出 `<TAG_0> 译文 <TAG_0>`（重复）时集合相等 → QC 通过 → merger 的 `str.replace` 把两处都还原成同一标签 → 成品标签重复。建议用 `collections.Counter` 对比计数：

```python
from collections import Counter
missing = Counter(orig_tags) - Counter(trans_tags)
extra = Counter(trans_tags) - Counter(orig_tags)
```

### 37. [P2] qc_checker.py:121-129 — 术语一致性检查是"子串包含"判定，误判/漏判双向存在
`tgt_term in stripped`：术语译法"田中"时，未翻译的"田中さん"反而通过检查（该情形已部分被日文残留检测兜住，但不含假名的未翻译片段漏网）；`src_term in original.text` 不归一化空白/断行，术语跨占位符或被断行拆开时漏检 → 该问题分级不触发重翻。

**修复建议**：src_term 匹配前对 original.text 做空白归一化；tgt 命中后检查术语前后是否仍是原文片段（与日文残留检测联动），至少在注释里写明该检查仅为"提示"不可作为放行依据（当前 break 后只 append 一条，行为已是提示级，保持）。

### 38. [P3] qc_checker.py:31-37 — 长度不一致时仍按短侧静默截断
已 log.warning（好于裸 zip），但公共 API 的调用方无法从返回值得知发生了截断；建议在首个 result 上附加一条 qc_issue 或返回截断计数。

### 39. [P3] qc_checker.py:116-117 — "译文过短"用含占位符的原文长度做阈值
`original.text` 是含 `<TAG_N>` 的文本，占位符把 2 字原文撑到 >3 字符 → 单字译文误报"译文过短"。建议比较前 `re.sub(r"<TAG_\d+>", "", original.text)`。

---

## 七、corpus_manager.py（术语库/语料库管理）

### 40. [P1] corpus_manager.py:79-83 — 固定临时文件名导致并发 `_save` 互相踩踏，术语库 JSON 可被损坏（用户术语丢失）
所有实例写**同一个** `corpus_vtuber.json.tmp`。GUI 线程实例 A 与 worker 侧实例 B（prompt_builder 因 mtime 变化重建后写）并发 `_save`：A `write_text` 写到一半，B `write_text` 截断重写 → A 的 `os.replace` 把**半截内容**换名成正式文件 → JSON 损坏；此后每次启动 `_load` 解析失败按空表处理（防投毒逻辑只保护缓存不保护文件），**用户全部强制术语静默清空**。

**修复建议**：

```python
import tempfile, threading
fd, tmp_path = tempfile.mkstemp(
    dir=str(self._corpus_path.parent),
    prefix=f"{self._corpus_path.stem}.", suffix=".tmp",
)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp_path, self._corpus_path)
except OSError:
    Path(tmp_path).unlink(missing_ok=True)
    raise
```

### 41. [P2] corpus_manager.py:74-85 — `_save` 无异常处理，Windows 下 `os.replace` 被杀软/OneDrive 短暂锁定时直接向 GUI 抛 PermissionError
`add/remove/update_all` 由 GUI 对话框直接调用，未捕获 → 弹栈/操作失败无提示。建议 `_save` 捕获 `OSError`，重试一次（sleep 50ms）后仍失败则降级为直接写 + `log.error`，并向调用方返回 bool。

### 42. [P2] corpus_manager.py:57-71 + 95-110 — 已存在实例的"读-改-写"窗口内发生丢失更新
类级 `_terms_cache` 只保证**新建**实例读到最新数据；长期存活的实例（如 GUI 持有的 manager）在 worker 侧写入后仍持旧快照，其后的 `add()` 会用旧快照**整表覆盖**，worker 刚写入的术语丢失。建议 `add/remove/update_all` 写盘前重新 stat + `json.loads` 对比 mtime，把磁盘上更新的条目合并进来（last-writer-wins 改为字段级合并），或收敛为"全局单例 + 写锁"。

### 43. [P2] corpus_manager.py:126-136 — `get_preset_terms` 失败时把空 dict 永久写入类级缓存（与 `_load` 的防投毒策略相悖）
一次瞬时读失败（文件被占用、编码异常）→ 整个进程生命周期预设术语消失且不再重试。`_load`（L66-69）特意注释了"解析失败不能把空表写进缓存"，此处却相反。建议 except 分支不写缓存直接返回空 dict。

### 44. [P3] corpus_manager.py:55 — 非 frozen 模式固定写项目内 `resources/`
开发/绿色安装目录只读时 `_save` 抛 PermissionError；建议非 frozen 也优先用户数据目录，resources 仅作初始种子。

### 45. [P3] corpus_manager.py:51 — 首次复制初始语料库非原子
`write_bytes` 中途崩溃留下半截 JSON，下次启动按空表处理且不自愈。建议同样走 tmp + `os.replace`。

### 46. [P3] corpus_manager.py:107-110 — `update_all` 静默丢弃空 key/value 行
批量导入时空译文行无声消失，建议 log.info 记录过滤数量。

---

## 八、问题总数统计

| 严重度 | 数量 | 问题编号 |
|---|---|---|
| P0（丢数据/崩溃） | 0 | — |
| P1（功能错误） | 3 | #8、#14、#40 |
| P2（健壮性缺陷） | 22 | #1、#2、#3、#9、#10、#13、#15、#16、#17、#18、#19、#24、#25、#26、#27、#31、#32、#36、#37、#41、#42、#43 |
| P3（代码质量） | 21 | #4、#5、#6、#7、#11、#12、#20、#21、#22、#23、#28、#29、#30、#33、#34、#35、#38、#39、#44、#45、#46 |
| **合计** | **46** | |

按文件分布：

| 文件 | P0 | P1 | P2 | P3 | 小计 |
|---|---|---|---|---|---|
| core/chunker.py | 0 | 0 | 3 | 4 | 7 |
| core/merger.py | 0 | 1 | 2 | 2 | 5 |
| core/subtitle_io.py | 0 | 1 | 6 | 4 | 11 |
| core/extractor.py | 0 | 0 | 4 | 3 | 7 |
| core/tag_handler.py | 0 | 0 | 2 | 3 | 5 |
| core/qc_checker.py | 0 | 0 | 2 | 2 | 4 |
| core/corpus_manager.py | 0 | 1 | 3 | 3 | 7 |
| **合计** | **0** | **3** | **22** | **21** | **46** |

## 九、审计中确认健康的关键路径（供回归测试锚定）

1. chunker 各分支 `current_idx` 必然推进，无死循环；空输入提前返回；gap 断点截断与 `current_idx` 一致。
2. overlap 条目在 prompt 编号、`parse_response` 截取、QC/translator 位置配对三处一致，批次边界无序号错位。
3. `<TAG_10>` 对 `<TAG_1>` 的 replace 无前缀碰撞（闭合 `>` 保证）。
4. CRLF/LF、UTF-8 BOM、编号不连续、逗号/点号毫秒、ASS `H:MM:SS.CC`、VTT NOTE/STYLE/cue 设置均由 pysubs2 正确处理；SRT 内联 `<i>/<b>/<u>/<font>` 经 pysubs2 → 占位符链路保留，不丢失。
5. extractor 无 subprocess/系统 ffmpeg 依赖（PyAV 内置），whisper 缺失预检与失败信号路径完整。
