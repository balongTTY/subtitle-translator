# 字幕翻译工具

> 基于 LLM 的 vtuber/直播字幕自动翻译工具——拖入 SRT/ASS/VTT，一键「烤肉」。

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![PyQt5](https://img.shields.io/badge/GUI-PyQt5-green)](https://pypi.org/project/PyQt5/)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)
[![Tests](https://github.com/balongTTY/subtitle-translator/actions/workflows/tests.yml/badge.svg)](https://github.com/balongTTY/subtitle-translator/actions/workflows/tests.yml)

---

## 为什么你需要这个工具

如果你在给 vtuber 录播做「烤肉」（翻译字幕），你会遇到这些痛苦：

1. **术语不统一**——同一个「配信」，前五分钟翻成「直播」，后五分钟翻成「放送」，观众困惑
2. **翻译腔严重**——「我觉得今天真的很开心呢」「请大家多多指教的说」——中文母语者一听就知道是翻译
3. **LLM 不懂圈子约定**——「スパチャ」圈内译作「SC」，但 LLM 翻成「超级留言」「醒目留言」
4. **手动打轴太累**——有了字幕文件也要一条条翻译，几百条翻到眼花
5. **上下文断裂**——LLM 只看 20 条一批，不知道这段是在打 boss 还是读评论

**这个工具解决以上所有问题。**

---

## 目录

1. [它能做什么](#它能做什么)
2. [快速开始](#快速开始)
3. [图文教程：第一次翻译](#图文教程第一次翻译)
4. [字幕提取：从视频直接生成字幕](#字幕提取从视频直接生成字幕)
5. [翻译引擎——三阶段流水线详解](#翻译引擎三阶段流水线详解)
6. [核心概念：术语库（语料库）](#核心概念术语库语料库)
7. [提示词工程——它为什么翻得好](#提示词工程它为什么翻得好)
8. [支持的 LLM 及配置](#支持的-llm-及配置)
9. [设置项完整参考](#设置项完整参考)
10. [项目架构](#项目架构)
11. [数据流详解](#数据流详解)
12. [开发者指南](#开发者指南)
13. [测试](#测试)
14. [性能与成本](#性能与成本)
15. [故障排查](#故障排查)
16. [打包分发](#打包分发)
17. [常见问题](#常见问题)
18. [贡献指南](#贡献指南)
19. [许可证与致谢](#许可证与致谢)

---

## 它能做什么

### 核心能力

| 能力 | 说明 |
|------|------|
| **字幕提取** | 拖入视频/音频（mp4/mkv/mp3/wav…），用 whisper 语音识别直接生成字幕——日语/英语优化，本地识别不上传 |
| **三阶段翻译** | 全篇分析 → 分批翻译 → 逐批质检，不是简单的「丢给 LLM 翻」 |
| **全篇上下文分析** | 翻译前 LLM 先通读整份字幕，产出话题分段、术语表、说话人特征、风格指南 |
| **术语强制库** | 你说了算——「スパチャ→SC」一旦定义，LLM 绝不用别的翻译；可一键从分析结果导入 |
| **源语言自动检测** | 日语/英语字幕混合也没关系，按内容自动判定每个文件的语言 |
| **翻译腔消除** | 内建 10+ 对正反例、6 类翻译规则、强制无标点输出 |
| **增量预览** | 翻译过程中实时看到结果；多文件并行时下拉切换查看各自结果 |
| **质检+自动重翻** | 日文残留、占位符缺失、原文泄漏——检测到就自动重试 |
| **ASS 标签保护** | `{\i1}{\b1}{\fnXXX}` 等格式标签在翻译过程中自动保管还原，绘图/音效条目直接跳过 |
| **批量并行** | 多文件同时翻译，可配置并行数 |
| **单条重译 + 编辑持久化** | 右键任意一条重新翻译；手动修改可一键保存回 `_zh` 文件 |
| **术语深度优化** | 一键让 LLM 逐条审视术语表——翻译自然吗？圈子用这个说法吗？缺了什么？ |

### 支持的格式

- **翻译输入**：SRT（SubRip）、ASS（Advanced SubStation Alpha）、SSA、VTT（WebVTT）
- **提取输入**：mp4 / mkv / avi / mov / wmv / flv / webm / m4v / ts 视频，mp3 / wav / m4a / aac / flac / ogg / opus / wma 音频
- **输出**：同格式，文件名加 `_zh` 后缀（如 `episode.srt` → `episode_zh.srt`）；提取产物为同名 `.srt`（如 `录播.mp4` → `录播.srt`）

### 翻译方向

- 日语 → 简体中文（主要场景）
- 英语 → 简体中文
- 日语/英语 → 繁体中文

---

## 快速开始

### 系统要求

- **Python**：3.10 或更高
- **操作系统**：Windows 10/11（主要测试平台）；macOS/Linux 理论上可运行但未测试
- **网络**：需要访问所配置 LLM 的 API 端点

### 安装

```bash
git clone https://github.com/your-username/subtitle-translator.git
cd subtitle-translator
pip install -r requirements.txt
```

`requirements.txt` 内容：

```
pysubs2>=1.8.1        # 字幕文件解析（SRT/ASS/VTT）
openai>=2.0.0         # OpenAI / DeepSeek / 自定义 API
anthropic>=0.40.0     # Claude API
tiktoken>=0.7.0       # Token 估算
cryptography>=42.0.0  # API 密钥加密（非 Windows 环境兜底；Windows 用系统 DPAPI）
darkdetect>=0.8.0     # 系统明暗主题检测
PyQt5>=5.15.11        # GUI 框架
faster-whisper>=1.0.0 # 字幕提取（whisper 语音识别，含 PyAV 音频解码）
```

### 启动

```bash
python main.py
```

---

## 图文教程：第一次翻译

### 第一步：配置 API

打开应用 → 菜单栏 **设置 → 设置** → **API 密钥** 标签页。

1. 在「LLM 提供商」下拉框中选择你要用的服务（如 DeepSeek）
2. 在「API Key」输入框中粘贴你的密钥
3. 「API 端点」会自动填入该提供商的默认地址
4. 点击右下角 **「测试连接」** 验证是否配置正确

> **提示**：测试连接**不会**永久保存你的密钥——它只是验证连接是否正常。点「取消」会恢复旧配置。

### 第二步：模型和语言设置

切换到 **模型** 标签页：

1. 「模型」下拉框选择模型（如 `deepseek-v4-flash`）
2. 「上下文预设」下拉框选 `1M`（或直接调「自定义数值」）
3. 「温度」保持默认 `0.3`（越低越稳定，越高越有创造性）

切换到 **语言** 标签页：

1. 源语言选「日语」
2. 目标语言选「简体中文」

切换到 **翻译选项** 标签页：

1. 「分析模式」选「全文发送」（DeepSeek 1M 上下文足够）
2. 其余保持默认

点击 **确定** 保存。

### 第三步：加载字幕

1. 直接把 SRT/ASS 文件**拖拽**到左侧文件列表区域
2. 或者点击「添加文件」按钮手动选择
3. 文件前有勾选框——可以只翻译部分文件

文件状态列会显示当前进度：「待翻译」→「分析中」→「待确认分析」→「翻译中」→「✓ 完成」

### 第四步：开始翻译

勾选要翻译的文件 → 点击顶部工具栏 **「▶ 开始翻译」**。

此时发生：

1. 底部状态栏显示进度
2. LLM 正在做全篇分析（通常 5~30 秒）
3. 分析完成后**自动暂停**，左侧出现分析面板

### 第五步：审核分析结果

左侧「全篇分析」面板展示了 LLM 分析出的：

- **话题分段**（带时间线和关键词）——双击可编辑
- **术语表**（源术语 → 译法）——双击弹出编辑框，也可直接 `+` 添加
- **翻译风格指南**——文本框直接修改

你可以在这里：
- 修改任何一个术语的翻译
- 删掉不对的话题
- 调整风格指南
- 点击「深度优化术语」让 LLM 再次审视

确认无误后，点击 **「确认分析，继续翻译 →」**。

如果想跳过分析，点 **「跳过分析，直接翻译」**。

> **批量处理多个文件**：多文件并行时，分析面板会逐个展示每个待确认的文件。点击 **「确认全部」** 会：先用你修改后的分析确认当前文件，再自动确认其余所有待确认文件——一次操作放行整批翻译，不用逐个点击。每个文件仍然保留各自的术语表和风格指南。

### 第六步：查看结果

右侧「对照预览」面板实时显示翻译进度：

- `⏳` = 等待翻译（灰色）
- `✓` = 翻译通过
- `⚠` = 需人工审核（黄色高亮）
- `✗` = 翻译失败

你可以**直接在预览表格中编辑译文**——双击单元格修改。右键菜单可复制原文/译文，或将 `⚠` 条目标记为已审核。

### 第七步：导出

1. 翻译完成后，结果自动保存为 `原文件名_zh.原扩展名`（在源文件同目录）
2. 想导出到别的文件夹 → 勾选文件 → 点击 **「导出所选」** → 选择目标目录

---

## 字幕提取：从视频直接生成字幕

没有现成字幕？拖入录播视频，工具用 **faster-whisper**（开源 MIT，CTranslate2 后端）在本地做语音识别，直接产出可翻译的 SRT。**音频不出本机**。

### 使用流程

1. 把视频/音频（mp4、mkv、mp3、wav 等）拖入左侧文件列表——状态显示「待提取」
2. 勾选后点击工具栏 **「✎ 提取字幕」**
3. 首次使用会自动下载识别模型（**下载进度实时显示在底部进度条**），提取过程中文件列表实时显示百分比
4. 完成后自动在视频同目录生成 `录播.mp4 → 录播.srt`，并**自动加入文件列表**——直接点「▶ 开始翻译」走正常烤肉流程

> **不想等首次下载**：设置 → 字幕提取 → 「⬇ 下载 / 检查模型」可**一键预下载**当前选中的模型（带独立进度条；已缓存会直接提示就绪）。国内网络请先把「模型下载源」切到 hf-mirror.com 镜像。

从视频到熟肉的完整链路：**拖入视频 → 提取字幕 → 确认分析 → 翻译 → `_zh.srt`**。

### 识别引擎：本地 or 在线 API

**设置 → 字幕提取 → 识别引擎** 二选一：

| 引擎 | 适用 |
|------|------|
| **本地 faster-whisper**（默认） | 音频不出本机；首次下载模型后离线可用；长录播成本为零 |
| **在线语音识别 API** | 免下载模型、速度快；音频会上传到所配置的服务 |

在线服务预设（都是 OpenAI SDK 兼容调用，填 API Key 即用）：

| 服务 | 端点 | 预设模型 | 时间轴 |
|------|------|----------|--------|
| OpenAI 官方 | api.openai.com/v1 | whisper-1 / gpt-4o-transcribe / gpt-4o-mini-transcribe | whisper-1 带 ✓ |
| Groq | api.groq.com/openai/v1 | whisper-large-v3 / whisper-large-v3-turbo | 带 ✓ |
| 硅基流动 | api.siliconflow.cn/v1 | FunAudioLLM/SenseVoiceSmall / Tencent/FireRedASR-AED-L | 带 ✓ |
| 小米 MiMo | api.xiaomimimo.com/v1 | mimo-v2.5-asr | 纯文本 → 估算 |
| 自定义 | 任意 | 手填 | 视模型 |

- **whisper 系模型返回带时间轴的分段**（字幕质量最佳）；文字型模型（MiMo、gpt-4o-transcribe）只返回文本，工具会**按句切分 + 时长比例均分**估算时间轴，完成后状态栏会提示「时间轴为估算值」
- MiMo 走 Chat Completions + base64 `input_audio` 协议（参照小米官方文档），语言参数自动映射（en→en，日语/自动→auto）
- **长音频自动切块**：超过服务单请求上限（MiMo 10MB / 其他 25MB）时按时间窗口重编码（mono 16kHz mp3）分次识别后拼接时间轴，一小时录播也能一次提取
- 在线 Key 独立存储（同样走 DPAPI 加密），与翻译用的 LLM Key 互不影响；「测试 Key」按钮用 /models 轻量端点验证

### 模型选择（本地引擎，专攻日语/英语）

| 模型 | 大小 | 适用 |
|------|------|------|
| `large-v3-turbo`（默认） | ~1.6GB | **日/英通吃，速度与精度最佳平衡，首选** |
| `large-v3` | ~3GB | 最高精度，长直播/嘈杂环境 |
| `kotoba-tech/kotoba-whisper-v2.0-faster` | ~1.5GB | **日语专用优化**（ReazonSpeech 数据训练，纯日语内容更快更准） |
| `medium` / `small` / `base` / `tiny` | 1.5GB~75MB | 显存/内存紧张时的降级选项 |

- 识别语言可选「自动检测 / 日语 / 英语」——自动检测按文件逐个判定，日英混合的列表不用分开处理
- VAD 静音过滤默认开启：长直播的静音段不产字幕，时间轴更干净
- 模型设置在 **设置 → 字幕提取** 标签页；国内网络请在「模型下载源」选 **hf-mirror.com 镜像**

### 性能参考

| 硬件 | large-v3-turbo 实时率（处理时长/视频时长） |
|------|------|
| CPU 8 线程（int8） | 约 0.3~0.6× |
| NVIDIA GPU（float16） | 约 0.05~0.15× |

> 一小时录播：GPU 约 3~9 分钟出全部字幕；CPU 约 18~36 分钟。提取一次，翻一辈子。

### 技术说明

- 识别在独立 QThread 中串行执行，与翻译流水线互斥（不会同时抢 API 和 CPU）
- 音频解码走 PyAV（faster-whisper 自带 FFmpeg 库），**无需系统安装 ffmpeg**
- **模型下载带真实进度**：逐文件走 huggingface_hub（字节级进度聚合到进度条）；配置镜像时自动禁用 Xet 传输（镜像站不代理 CAS 协议会 401），断点续传
- whisper 分段自动规整：换行折叠（避免破坏翻译提示词的编号行解析）、空段跳过、零时长修正、同名 `.srt` 已存在时自动备份 `.bak`

---

## 翻译引擎——三阶段流水线详解

这不是简单的「丢给 LLM 翻译」。整个流程分三个阶段，环环相扣：

### 阶段一：全篇分析（`core/analyzer.py`）

```
输入：整份字幕（或三段连续取样）
输出：AnalysisResult { topic_segments, speakers, glossary, style_guide }
```

LLM 接到一份**带思考步骤的提示词**，不是直接让它输出 JSON，而是先引导它：

1. **扫结构**：这场直播分几个阶段？时间间隔在哪？
2. **识说话人**：男的女的？什么语气？口癖？怎么读评论？
3. **揪术语**：对照着 90+ 词的参考清单，从字幕里找出需要统一翻译的词
4. **写风格指南**：口语化程度、敬语策略、情绪处理

提示词中的术语参考清单来自真实的烤肉圈用语（萌娘百科、B站专栏），覆盖 6 大类别。LLM 看到这些参考后会主动识别相似模式的词。

**分析完成后翻译暂停**，给你机会审核修改。这个步骤是翻译质量的基石——后面的分批翻译全靠这份分析。

### 阶段二：分批翻译（`core/chunker.py` + `core/translator.py`）

```
输入：字幕条目列表 + AnalysisResult
输出：翻译后的条目列表
```

**分块策略**：

- 每批 2000 tokens（可配置）
- 批间 **5 条 overlap**：每批末尾 5 条作为下一批的上下文（不计入该批翻译结果）
- 优先在 **>5 秒时间间隔**处断点（场景切换）
- overlap 条目不消耗主条目 token 预算

**每批的翻译提示词包含**：

```
## 基本规则（6 条，包括强制无标点）
## 日→中翻译要点
  ├── 主语省略
  ├── 句型转换（と思います砍掉、じゃない不硬翻）
  ├── 语气词处理（ね/よ/わ 不逐字翻）
  ├── 敬语策略（です・ます→自然中文）
  ├── 长句拆分（每行 ≤25 字）
  ├── 游戏直播专用（惨叫、笑声、读评论）
  ├── 注释规范
  └── 红线（禁止项）
## 当前场景（话题上下文）
## 强制术语（语料库定义）
## AI 建议术语（LLM 分析生成）
```

### 阶段三：逐批质检（`core/qc_checker.py`）

每批翻译完成后自动检查：

| 检测项 | 标准 | 不通过时 |
|--------|------|----------|
| **空译文** | 译文字符数为 0 | 触发重翻 |
| **原文未翻译** | 译文 == 原文 | 触发重翻 |
| **占位符缺失** | `<TAG_N>` 数量不匹配 | 触发重翻 |
| **占位符多余** | 译文多出了原文没有的 TAG | 触发重翻 |
| **日文残留** | 假名残留比例 > 30% | 触发重翻 |
| **译文过短** | 原文 >3 字但译文 ≤1 字 | 触发重翻 |

- 严重问题（空译文/未翻译/缺占位符）超过 20% → **整批重翻**
- 重试最多 2 次（可配置）
- 2 次后仍不合格 → 标记 `⚠ 需审核`，继续下一批（不阻塞整体流程）

重翻时会附带**上一轮的问题清单**，告诉 LLM 具体哪里有问题。

---

## 核心概念：术语库（语料库）

### 为什么需要术语库

LLM 全篇分析生成的术语表存在两个问题：

1. **不懂圈子约定**：vtuber 粉丝圈把「スパチャ」叫「SC」，不是「醒目留言」；把「同接」叫「同接」，不是「同时在线人数」
2. **每次分析不稳定**：同一个词这次翻 A 下次翻 B

术语库解决这个问题——**你定义的翻译规则，LLM 必须遵守**。

### 优先级

```
语料库（强制） > 用户手工修改的分析结果 > LLM 全篇分析结果
```

### 如何使用

右侧标签页 → **「术语库」**：

1. 点击 `+ 添加` 输入源术语和译法
2. 双击单元格直接编辑
3. 选中行 → `- 删除`
4. 上方搜索框快速过滤

### 工作原理

1. **分析阶段**：语料库中的术语作为「已知术语」注入分析提示词——LLM 知道这些词已有固定翻译，不会在 glossary 中重复输出冲突版本
2. **翻译阶段**：术语表被拆分为「强制术语」（语料库中有的）和「AI 建议」（LLM 生成的）两层——强制术语标注「必须严格使用，不可自行修改」
3. **冲突解决**：如果 LLM 生成的 glossary 中有语料库中已有的词，语料库版本直接覆盖

### 与 `glossary_vtuber.json` 的区别

| 文件 | 用途 | 可编辑 | 优先级 |
|------|------|--------|--------|
| `corpus_vtuber.json` | **术语库**——你的强制规则 | ✓（UI 或编辑器） | 最高 |
| `glossary_vtuber.json` | **预设参考**——112 条烤肉术语 | ✓（编辑器） | 参考级 |

> 简单说：`corpus` 里的你说了算，`glossary` 里的只是给 LLM 看的参考清单。

> **预设参考的注入方式**：分析阶段全量注入分析提示词；翻译阶段只注入「本批字幕里实际出现」的预设词，控制 token 开销又不牺牲术语一致性。

### 推荐初始配置

```
スパチャ → SC
同接 → 同接
コラボ → 联动
配信 → 直播
初配信 → 出道直播
切り抜き → 切片
初見 → 新来的
草 → 草
www → www
```

---

## 提示词工程——它为什么翻得好

翻译质量的 80% 取决于提示词。这里记录各组件的设计思路。

### 1. 全篇分析提示词（日→中）

**位置**：`core/analyzer.py` → `ANALYSIS_PROMPT_JA`

**设计思路**：

- ❌ 不说「请分析以下内容并输出 JSON」→ LLM 不知道怎么做
- ✅ 给**四步思考流程**：扫结构 → 识说话人 → 揪术语 → 写风格指南
- ✅ 每步有**具体问题**引导（「主播是男的女的？从自称判断」）
- ✅ 第三步附带 **90+ 词分类参考清单**（直播/粉丝/游戏/弹幕/ACG）——不是让 LLM 抄，而是激活它对这类词的敏感度
- ✅ 给出**反面例子**：「枠→画框（错）」「凸待ち→等待凸起（错）」
- ✅ JSON 示例非空——每个字段都填了真实数据，LLM 会模仿这个格式和详略程度

### 2. 翻译提示词（日→中）

**位置**：`services/prompt_builder.py` → `_ja_to_zh_guidelines()`

**设计思路**：

- ❌ 不说「避免翻译腔」
- ✅ 给 **10 对正反例**，每对是「❌ 翻译腔 → ✅ 自然中文」
- ✅ 规则分组：主语 / 句型 / 语气词 / 敬语 / 长句 / 游戏直播 / 注释 / 红线
- ✅ 每条规则带**具体例子**：「〜と思います→直接砍掉，不是『我觉得』」
- ✅ 核心心法放在最前：「让观众感觉不到翻译的存在」「代入身边的朋友」
- ✅ **强制无标点**：规则第 6 条 + 红线第 1 条 + 所有示例都不带标点

### 3. 术语深度优化提示词

**位置**：`core/analyzer.py` → `GLOSSARY_OPTIMIZE_PROMPT`

**设计思路**：

- 四个审视维度：自然度 / 领域准确性 / 缺失检查 / 冗余清理
- 每个维度都有**正确和错误的例子**
- 附上原始字幕片段作为上下文参考

### 4. 分析提示词 + 语料库联动

分析提示词末尾会注入**已知术语表**（来自语料库），告诉 LLM：「这些词已有固定翻译，请直接在你的 glossary 中用这些翻译，不要自创」。这解决了 LLM 重复输出已有翻译的问题。

---

## 支持的 LLM 及配置

### 各提供商对比

| | OpenAI | Anthropic Claude | DeepSeek | 自定义 |
|---|---|---|---|---|
| **默认模型** | gpt-5-mini | claude-sonnet-5 | deepseek-v4-flash | 自行输入 |
| **上下文窗口** | 400K（gpt-5）/ 1M（gpt-4.1） | 200K（Claude 5）/ 1M（Fable 5） | 1M | 自行配置 |
| **中文质量** | ★★★★ | ★★★★ | ★★★★★ | — |
| **速度** | ★★★ | ★★★ | ★★★★ | — |
| **成本(百万token)** | 输入$2.5/输出$10 | 输入$3/输出$15 | 输入¥1/输出¥2 | — |
| **Prompt Caching** | ✓ 自动 | ✓ 需标记 | — | — |
| **推荐分析模式** | 智能取样 | 智能取样 | 全文发送 | 视情况 |

### 各提供商配置示例

**DeepSeek**（推荐，性价比最高）：

```
提供商: DeepSeek
模型: deepseek-v4-flash
API 端点: https://api.deepseek.com
上下文窗口: 1M (1000000)
温度: 0.3
分析模式: 全文发送
分析阶段预留: 64000
```

> 注意：DeepSeek 端点 **不需要 `/v1` 后缀**。`deepseek-chat` 和 `deepseek-reasoner` 即将弃用，请使用 `deepseek-v4-flash` 和 `deepseek-v4-pro`。

**OpenAI**：

```
提供商: OpenAI
模型: gpt-5-mini
API 端点: https://api.openai.com/v1
上下文窗口: 400000
温度: 0.3
分析模式: 智能取样
```

**Claude**：

```
提供商: Anthropic (Claude)
模型: claude-sonnet-5
API 端点: （留空使用官方）
上下文窗口: 200000
温度: 0.3
分析模式: 智能取样
```

**Ollama / 本地模型**：

```
提供商: 自定义 OpenAI 兼容 API
模型: qwen2.5:7b（或你的模型名）
API 端点: http://localhost:11434/v1
上下文窗口: 4096
温度: 0.3
分析模式: 智能取样
```

### 各提供商可用的模型列表

**OpenAI**：gpt-5 / gpt-5-mini / gpt-4o / gpt-4o-mini / gpt-4.1 / gpt-4.1-mini / gpt-4.1-nano / gpt-4-turbo

**Anthropic**：claude-fable-5 / claude-opus-5 / claude-sonnet-5 / claude-haiku-4-5-20251001 / claude-sonnet-4-20250514 / claude-opus-4-20250514 / claude-haiku-4-20250514

**DeepSeek**：deepseek-v4-flash / deepseek-v4-pro / deepseek-chat（旧名兼容）/ deepseek-reasoner（旧名兼容）

> 模型下拉框可手动输入任何模型名（不限于列表）。

**自定义**：自行输入任何模型名

---

## 设置项完整参考

### API 密钥（Tab 1）

| 设置 | 说明 |
|------|------|
| LLM 提供商 | OpenAI / Anthropic / DeepSeek / 自定义 |
| API Key | 密码输入框，看不到明文（已保存的显示 `••••••••`） |
| API 端点 | 自动填入默认端点，可手动修改 |
| 测试连接 | 临时用当前输入验证连接（**不永久保存**，Cancel 会恢复旧值） |

> **安全说明**：API Key 在 Windows 上用 **DPAPI**（当前用户域加密）存储在 `%APPDATA%/SubtitleTranslator/keys.enc`，绑定当前用户账户，无法复制到其他机器/用户使用，磁盘上无明文。旧版本（Fernet 明文密钥文件）首次启动时自动迁移。每个提供商的密钥独立存储。

### 模型（Tab 2）

| 设置 | 默认值 | 说明 |
|------|--------|------|
| 模型 | 取决于提供商 | 下拉框包含常用模型 + 可手动输入 |
| 上下文预设 | — | 快捷选项：4K/8K/16K/32K/64K/128K/200K/1M/自定义 |
| 自定义数值 | 64000 | 精确的上下文窗口上限（tokens） |
| 温度 | 0.3 | 0.0~2.0，越低越稳定 |
| 最大重试 | 3 | API 调用失败后的重试次数 |

### 语言（Tab 3）

| 设置 | 选项 |
|------|------|
| 源语言 | 日语 / 英语 / 自动检测 |
| 目标语言 | 简体中文 / 繁体中文 |

### 翻译选项（Tab 4）

| 设置 | 默认值 | 说明 |
|------|--------|------|
| 每批目标大小 | 2000 tokens | 预设：500/1000/2000/3000/5000 |
| Overlap 条数 | 5 | 预设：0/3/5/10 |
| 分析阶段预留 | 16000 tokens | 预设：4K~1M |
| 分析模式 | 智能取样 | 全文发送 / 智能取样 |
| 并行文件数 | 2 | 1~8，多个文件同时翻译 |
| 质检最大重试 | 2 | 单批翻译最多重试几次 |

> 并行翻译基于多 QThread worker：多个文件同时分析/翻译，但**共享同一个速率限制器**（不会因为并行而超发 API 配额）。每个文件的分析结果仍需你在左侧面板确认后才会继续。

### 外观（Tab 5）

| 设置 | 说明 |
|------|------|
| 背景颜色 | 取色器选择任意颜色，或输入 hex 值 |
| 窗口透明度 | 30%~100% 滑块 |
| 背景图片 | PNG/JPG 居中显示 |
| 预览效果 | 实时预览当前设置 |
| 恢复默认 | 清除所有自定义外观 |

### 字幕提取（Tab 6）

| 设置 | 默认值 | 说明 |
|------|--------|------|
| 识别引擎 | 本地 faster-whisper | 本地离线 / 在线 API 二选一 |
| 识别语言 | 自动检测 | 自动检测 / 日语 / 英语，按文件逐个判定（本地/在线共用） |
| Whisper 模型 | large-v3-turbo | 下拉可选，也可手填本地路径/HF 仓库名（如 kotoba-whisper） |
| 运行设备 | 自动 | 自动 / CPU / CUDA（NVIDIA GPU） |
| 计算精度 | 自动 | int8（CPU 推荐）/ float16（GPU 推荐）/ float32 |
| CPU 线程数 | 自动 | 0 = 自动 |
| VAD 过滤 | 开启 | 过滤静音段，长直播推荐开启（仅本地引擎） |
| 模型下载源 | 官方 | 国内网络建议选 hf-mirror.com 镜像（仅本地引擎） |
| 在线服务 | OpenAI 官方 | OpenAI / Groq / 硅基流动 / 小米 MiMo / 自定义，切换自动填端点和模型预设 |
| 在线识别模型 | whisper-1 | 按服务预设，可手填 |
| 在线 API Key | — | 独立 DPAPI 加密存储；「测试 Key」可验证 |

---

## 项目架构

### 分层结构

```
┌─────────────────────────────────────────┐
│  main.py  —— 入口                       │
├─────────────────────────────────────────┤
│  gui/  —— 用户界面层                    │
│  ├── main_window.py    主窗口           │
│  ├── file_panel.py     文件列表         │
│  ├── preview_panel.py  预览面板         │
│  ├── analysis_panel.py 分析编辑面板     │
│  ├── corpus_panel.py   术语库面板       │
│  ├── settings_dialog.py 设置对话框      │
│  ├── theme_manager.py  主题管理器       │
│  ├── progress_widget.py 进度条          │
│  └── editor_dialog.py  条目编辑弹窗     │
├─────────────────────────────────────────┤
│  core/  —— 业务逻辑层                   │
│  ├── translator.py     翻译编排器       │
│  ├── extractor.py      字幕提取(faster-whisper) │
│  ├── asr_online.py     在线ASR引擎      │
│  ├── audio_splitter.py 音频切块         │
│  ├── model_download.py 模型下载         │
│  ├── analyzer.py       全篇分析器       │
│  ├── chunker.py        智能分块器       │
│  ├── qc_checker.py     质检器           │
│  ├── merger.py         合并器           │
│  ├── corpus_manager.py 语料库管理器     │
│  ├── tag_handler.py    ASS/SRT标签处理  │
│  ├── translation_batch.py 数据类        │
│  └── subtitle_io.py    字幕文件读写     │
├─────────────────────────────────────────┤
│  services/  —— 外部服务层               │
│  ├── base_llm.py       抽象接口         │
│  ├── openai_service.py OpenAI兼容实现   │
│  ├── claude_service.py Claude实现       │
│  ├── prompt_builder.py 提示词构建器     │
│  └── rate_limiter.py   限流器           │
├─────────────────────────────────────────┤
│  config/  —— 配置层                     │
│  │   └── settings.py   QSettings封装    │
│  utils/   —— 工具层                     │
│  │   ├── constants.py  常量             │
│  │   ├── logger.py     日志             │
│  │   └── token_counter.py Token估算     │
│  resources/  —— 资源文件               │
│  │   ├── corpus_vtuber.json  术语库     │
│  │   ├── glossary_vtuber.json 参考表    │
│  │   └── styles/           QSS主题      │
│  tests/  —— 测试                       │
│  │   ├── test_*.py      pytest单元测试  │
│  │   └── e2e/           端到端验证脚本  │
│  docs/  —— 文档与修复记录              │
└─────────────────────────────────────────┘
```

### 仓库根目录

| 文件 | 说明 |
|------|------|
| `main.py` | 程序入口 |
| `pyproject.toml` | 项目元数据 + 依赖 + pytest/ruff 配置 |
| `requirements.txt` | 运行依赖（与 pyproject 保持一致） |
| `subtitle_translator.spec` | PyInstaller 打包配置 |
| `README.md` / `README.en.md` | 中文 / 英文说明 |
| `CHANGELOG.md` | 版本变更日志 |
| `CONTRIBUTING.md` | 贡献指南 |
| `CODE_OF_CONDUCT.md` | 行为准则 |
| `LICENSE` | MIT 许可证 |

### 关键设计决策

**为什么用 QThread 而不是 asyncio？**
PyQt5 的事件循环要求所有 GUI 操作在主线程。翻译是 I/O 密集型（API 调用），放在 QThread worker 中执行。Worker 通过 Qt signals（自动跨线程队列）向主线程报告进度。这样 GUI 始终响应，翻译在后台运行。

**为什么翻译提示词不用 JSON 格式？**
编号行格式 `N. 译文` 比 JSON 更省 token、LLM 服从性更好、解析鲁棒性更强。JSON 容易出现语法错误（少一个逗号就全毁），编号行格式只需 `^(\d+)\.\s+(.*)$` 一个正则搞定。

**为什么 ASS 标签用占位符而不是直接翻译？**
LLM 不认得 `{\i1}` 之类的 ASS 标签，会把它们当普通文本处理。用 `<TAG_0><TAG_1>` 占位符替换，翻译后还原，确保格式标签毫发无损。绘图块 `{\p1}...{\p0}` 检测后直接跳过翻译。

**为什么语料库优先级高于 LLM 分析？**
LLM 每次分析可能给出不同的术语翻译（这次「スパチャ→SC」下次「スパチャ→醒目留言」）。语料库是用户的「唯一真相源」，无论分析结果如何，强制术语不可篡改。

---

## 数据流详解

### 翻译完整数据流

```
用户拖入文件
  │
  ▼
FilePanel ──filepaths──▶ MainWindow._on_translate()
  │
  ▼
TranslatorFacade.start_translation()
  │
  ├─► prepare_translation()  创建 N 个 QThread+Worker（N = 并行文件数，文件均分）
  ├─► 连接 Qt signals（Facade 聚合所有 worker 的信号转发给 UI）
  └─► start()  启动所有线程
        │
        ▼
      TranslatorWorker.run()   （每个 worker 各跑自己分配的文件）
        │
        ▼  阶段一
      SubtitleFile.load() ──entries──▶ SubtitleAnalyzer.build_analysis_prompt()
        │                                    │
        │                              LLM.analyze(prompt)
        │                                    │
        │                              parse_analysis_response()
        │                                    │
        │                         ┌─ AnalysisResult ─┐
        │                         │  · topic_segments │
        │                         │  · speakers       │
        │                         │  · glossary       │
        │                         │  · style_guide    │
        │                         └──────────────────┘
        │                                    │
        │                    ┌─── signal: file_analysis_done ───▶ GUI 展示
        │                    │
        │                    ├─── signal: waiting_for_approval ──▶ GUI 等待
        │                    │                                      │
        │                    │        用户修改分析 + 点击确认 ◀────┘
        │                    │         │
        │                    │  approve_analysis(filepath, edited)
        │                    │    Facade 按 filepath 路由到对应 worker
        │                    │         │
        │                    ▼         ▼
        │  阶段二          CorpusManager + AnalysisResult
        │                    │
        │              SubtitleChunker.chunk(entries, analysis)
        │                    │
        │              ┌── TranslationBatch[0]
        │              │    └── entries + topic_context + glossary
        │              ├── TranslationBatch[1]
        │              │    └── entries + topic_context + glossary (+ overlap)
        │              ├── ...
        │              └── TranslationBatch[N]
        │
        │  每批循环:
        │    prompt_builder.build_translation_prompt(batch)
        │      │
        │      ├── 基本规则 + 日→中翻译要点
        │      ├── 当前场景（话题上下文）
        │      ├── 强制术语（语料库）    ◀── CorpusManager.split_terms()
        │      └── AI 建议术语（LLM生成）
        │
        │    LLM.translate_batch(batch) ──▶ translated_texts
        │      │
        │      ▼  阶段三
        │    QCChecker.check(entries, translated_texts)
        │      │
        │      ├── 通过 ──▶ 下一批
        │      └── 不通过 ──▶ 重试(最多2次) ──▶ 仍不通过 ──▶ 标记 ⚠需审核
        │
        │    signal: batch_completed ──▶ GUI 增量更新预览
        │
        ▼
      Merger.merge(original, results) ──▶ 翻译后条目列表
        │
      SubtitleFile.save(output_path, merged)
        │
      signal: file_completed ──▶ GUI 显示完成
```

### 术语表数据流

```
corpus_vtuber.json ──load──▶ CorpusManager._terms
                                  │
                   ┌──────────────┤
                   │              │
                   ▼              ▼
           analyzer.py       prompt_builder.py
           (分析提示词中     (翻译提示词中
            告知LLM已有术语)   拆分强制/建议)
                   │              │
                   ▼              ▼
              LLM分析         LLM翻译
              glossary        batch翻译
```

---

## 开发者指南

### 添加新 LLM 提供商

以添加「智谱 GLM」为例：

**1. 注册提供商**

`utils/constants.py`：

```python
LLM_PROVIDERS = {
    ...
    "zhipu": "智谱 GLM",
}

PROVIDER_MODELS = {
    ...
    "zhipu": ["glm-4-plus", "glm-4-flash"],
}

DEFAULT_MODELS = {
    ...
    "zhipu": "glm-4-flash",
}

DEFAULT_API_BASE_URLS = {
    ...
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
}

MODEL_CONTEXT_WINDOWS = {
    ...
    "glm-4-plus": 128000,
    "glm-4-flash": 128000,
}
```

**2. OpenAI 兼容的直接复用**

智谱 API 兼容 OpenAI 格式 → 不需要新建 service，`OpenAIService` 天然支持。

用户只需在设置中选择「自定义 OpenAI 兼容 API」，填入端点和模型名即可。如果要让「智谱 GLM」作为独立提供商出现在下拉框中，按步骤 1 配置常量即可。

**3. 非 OpenAI 兼容的**

实现 `services/base_llm.py` 中的 `BaseLLMService` 接口：

```python
class ZhipuService(BaseLLMService):
    def translate_batch(self, batch, source_lang, target_lang) -> list[str]: ...
    def analyze(self, prompt: str) -> str: ...
    def validate_api_key(self) -> bool: ...
    
    @property
    def provider_name(self) -> str: return "zhipu"
    @property
    def default_model(self) -> str: return "glm-4-flash"
```

然后在 `core/translator.py` 的 `_get_llm_service()` 中添加分支。

### 关键类的接口

<details>
<summary><b>BaseLLMService</b>（抽象接口）</summary>

```python
class BaseLLMService(ABC):
    @abstractmethod
    def translate_batch(self, batch: TranslationBatch,
                        source_lang: str, target_lang: str) -> list[str]: ...
    @abstractmethod
    def analyze(self, prompt: str) -> str: ...
    @abstractmethod
    def validate_api_key(self) -> bool: ...
    
    @property
    @abstractmethod
    def provider_name(self) -> str: ...
    @property
    @abstractmethod
    def default_model(self) -> str: ...
```
</details>

<details>
<summary><b>TranslationBatch</b>（数据类）</summary>

```python
@dataclass
class TranslationBatch:
    batch_id: int              # 批次编号
    entries: list[SubtitleEntry]  # 全量条目（含 overlap）
    primary_start_idx: int     # 新条目起始（不含 overlap）
    primary_end_idx: int       # 新条目结束
    glossary: dict[str, str]   # 累计术语表
    topic_context: str         # 当前时段话题
    estimated_tokens: int      # Token 估算
```
</details>

<details>
<summary><b>AnalysisResult</b>（数据类）</summary>

```python
@dataclass
class AnalysisResult:
    topic_segments: list[dict]  # [{start_idx, end_idx, topic, key_terms}]
    speakers: list[dict]        # [{speaker, tone, speech_patterns}]
    glossary: dict[str, str]    # {日文: 中文}
    style_guide: str            # 翻译风格建议
```
</details>

<details>
<summary><b>CorpusManager</b>（术语库管理器）</summary>

```python
class CorpusManager:
    def get_all(self) -> dict[str, str]: ...
    def get(self, term: str) -> str | None: ...
    def add(self, term: str, translation: str) -> None: ...
    def remove(self, term: str) -> None: ...
    def update_all(self, terms: dict) -> None: ...
    def resolve_conflicts(self, llm_glossary: dict) -> dict: ...
    def split_terms(self, llm_glossary: dict) -> tuple[dict, dict]: ...
    @staticmethod
    def get_preset_terms() -> dict[str, str]: ...  # glossary_vtuber.json 参考表
```
</details>

### 线程模型

```
Main Thread                     Worker Threads (N 个 QThread, N=并行文件数)
     │                                │           │
     ├─ TranslatorFacade()            │           │
     ├─ prepare_translation() ──────► │  worker1  │  worker2 ...
     │   文件均分                       │           │
     ├─ connect signals ◄─────────── │  ────────► │  各 worker 的信号由
     │   (Facade 聚合转发)             │  ════════► │  Facade 转发给 UI
     ├─ start() ────────────────────► │  run()    │  run()
     │                                ├─ _translate_one_file()
     │  ◄── signal: file_started ────┤
     │  ◄── signal: analysis_done ───┤
     │  ◄── signal: waiting ─────────┤  [BLOCKED on _continue_event]
     │                                │
     │  approve_analysis(filepath,..)─►  Facade 按 filepath 路由到对应 worker
     │                                ├─ chunk + translate
     │  ◄── signal: batch_done ──────┤
     │  ◄── signal: file_done ───────┤
     │  ◄── signal: all_completed ───┤  Facade 汇总所有 worker 后只报一次
     │                                └─ run() returns
     └─ _on_all_completed()
```

> 每个文件的分析都需你在左侧面板确认（多个文件并行时面板逐个展示、逐个确认）。


### 调试

**查看日志**：

```
%APPDATA%/SubtitleTranslator/logs/translator.log
```

自动轮转，保留 5 个 2MB 文件。

**Python 调试模式**：

```bash
python -c "
import logging
logging.basicConfig(level=logging.DEBUG)
import sys; sys.path.insert(0, '.')
from core.analyzer import SubtitleAnalyzer
# ... 测试代码 ...
"
```

**查看生成的提示词**：

```python
from services.prompt_builder import build_translation_prompt
from core.translation_batch import TranslationBatch, SubtitleEntry

batch = TranslationBatch(...)
prompt = build_translation_prompt(batch, 'ja', 'zh-CN')
print(prompt)
```

---

## 测试

项目有两层测试：**单元测试**（pytest，`tests/`）与**端到端验证脚本**（`tests/e2e/`，使用 fake LLM，不触网、不花 token）。

### 单元测试

```bash
pip install -e ".[dev]"   # 安装 pytest
pytest                    # 149 项，约 10 秒
```

### 端到端验证

```bash
python tests/e2e/run_e2e.py          # 一键运行全部端到端脚本
python tests/e2e/test_parallel.py    # 并行翻译端到端（多文件并行 + 按文件确认 + 结果聚合）
python tests/e2e/test_parallel_gui.py # 通过主窗口真实触发翻译（信号连接 + 面板确认路由）
python tests/e2e/test_preview_parallel.py # 并行预览状态隔离 + 单条重译 + 保存回文件
python tests/e2e/test_real_files.py  # 真实风格文件鲁棒性（BOM/CRLF/HTML标签/多行/ASS绘图/样式保留）
python tests/e2e/test_parser.py      # LLM 输出解析鲁棒性 + JSON 尾逗号 + 批量确认
python tests/e2e/test_extractor.py   # 字幕提取（fake whisper：转换/备份/端到端/取消，不触网）
```

> 真实文件测试还验证了一个细节：pysubs2 保存 SRT 时会静默丢弃 `{\b}` 粗体标记，
> 工具已内置修复（保存前转回 `<b>/</b>`），`<i>/<u>` 由 pysubs2 原生保留。

> 注意：端到端脚本会读写本机真实设置（QSettings）和 `resources/corpus_vtuber.json` 语料库，
> 运行后可能留下测试痕迹。发布前建议用干净的测试环境。

### 手动功能测试清单

- [ ] 应用能正常启动（`python main.py`）
- [ ] 设置对话框所有 5 个标签页正常切换
- [ ] 各提供商切换时模型、端点、Key 正确切换
- [ ] 测试连接功能正常（需要有效 API Key）
- [ ] 拖拽 SRT/ASS 文件能添加到列表；「移除选中」可删单行
- [ ] 拖入视频/音频显示「待提取」；「✎ 提取字幕」能识别并生成同名 .srt 自动加入列表
- [ ] 提取过程中取消生效（当前识别调用结束后停止）
- [ ] 设置对话框「字幕提取」标签页各选项保存后生效
- [ ] 勾选框和文件列表状态正确
- [ ] 全篇分析能正常完成并显示结果
- [ ] 分析面板的编辑功能（话题、术语、风格指南）正常
- [ ] 术语深度优化按钮能触发并返回结果
- [ ] 分批翻译 + 增量预览正常；多文件并行时下拉切换查看各文件结果
- [ ] 质检重试机制正常
- [ ] 翻译结果正确保存为 `_zh` 文件
- [ ] 右键「重新翻译此条」生效并写回文件；「保存修改」生效
- [ ] 语料库「从分析导入」能把分析术语合入语料库
- [ ] 源语言选「自动检测」时日/英字幕都能正确翻译
- [ ] 导出功能正常
- [ ] 主题切换（浅色/深色）正常
- [ ] 自定义背景（颜色、透明度、图片）正常
- [ ] 术语库面板添加/编辑/删除正常
- [ ] 取消翻译功能正常

### 测试用字幕

项目桌面有测试文件 `vtuber_test.srt`——34 条日文字幕，模拟完整 vtuber 直播录播。

提取功能手动测试素材：`tests/e2e/_tts_en.wav`——11 秒英语真实语音（Windows TTS 生成），用 tiny 模型约 2 秒出 3 条字幕。

---

## 性能与成本

### Token 消耗估算

以 **100 条日文字幕**（约 2~3 分钟的直播片段）为例：

| 阶段 | Token 消耗 | 说明 |
|------|-----------|------|
| 全篇分析 | 5K~30K | 取决于字幕长度和分析模式 |
| 每批翻译 | 2K~5K | 取决于 batch size 配置 |
| 翻译总数 | 约 4~8 批 | 100条 ÷ 20条/批 ≈ 5批 |
| **合计** | **约 30K~50K tokens** | — |

### 各模型成本对比（以 100 条字幕为例）

| 模型 | 输入成本 | 输出成本 | **小计** |
|------|----------|----------|----------|
| DeepSeek V4 Flash | ¥0.03 | ¥0.06 | **≈ ¥0.09** |
| GPT-4o | $0.08 | $0.30 | **≈ $0.38** |
| GPT-4o-mini | $0.005 | $0.03 | **≈ $0.035** |
| Claude Sonnet 4 | $0.09 | $0.45 | **≈ $0.54** |

> DeepSeek V4 Flash 是最经济的选择——翻译 1000 条字幕不到 ¥1。

### 速度估算

| 模型 | 分析耗时 | 翻译耗时/批 | 100 条总耗时 |
|------|----------|-------------|-------------|
| DeepSeek V4 Flash | 5~15s | 3~8s | **30~90s** |
| GPT-4o | 10~25s | 5~15s | **60~180s** |
| GPT-4o-mini | 8~15s | 3~8s | **40~100s** |
| Claude Sonnet 4 | 10~20s | 8~20s | **80~200s** |

---

## 故障排查

| 症状 | 可能原因 | 解决方法 |
|------|----------|--------|
| 提取时报 `cublas64_12.dll`/CUDA 相关错误 | 有 NVIDIA 驱动但缺 CUDA 运行库 | 工具会**自动回退 CPU** 并在日志中告警；如需 GPU 请安装 CUDA 12 运行库，或设置中把运行设备改为 CPU |
| 提取时程序崩溃（段错误） | ctranslate2 与 Qt5 的 DLL 加载顺序冲突 | `main.py` 已内置预导入守卫，正常启动不会遇到；仅绕过 `main.py` 自行组装进程时需先 `import ctranslate2` 再导入 PyQt5 |
| 启动后文件状态一直「待翻译」 | 信号未正确连接 | 重启应用 |
| 点击开始翻译后无反应 | 1) 未配置 API Key 2) 旧线程未清理 | 1) 检查设置中的 API Key 2) 重启应用 |
| 分析完成后不继续 | **正常行为**——翻译在等你在左侧面板点「确认继续翻译」 | 检查分析结果 → 点击按钮继续 |
| 翻译到一半卡住 | API 调用超时或限流 | 等待（最多 90s × 3 次重试），或点「取消」后重试 |
| 翻译腔严重 | 术语库未配置 / LLM 模型能力不足 | 1) 配置语料库常用术语 2) 用术语深度优化 3) 换更强模型 |
| 连接测试失败 | Key 错误 / 端点错误 / 网络问题 | 检查 Key 是否正确、端点是否可访问 |
| 设置对话框 Cancel 后设置仍变了 | 测试连接按钮会临时改设置 | 已修复：测试连接后自动恢复旧值 |
| ASS 文件翻译后格式丢失 | pysubs2 解析异常 | 应用自动探测 UTF-8/GBK/Shift-JIS 编码；仍失败时手动另存为 UTF-8 |
| 加载非 UTF-8 字幕报错 | 极少数无法识别的编码 | 用文本编辑器转存为 UTF-8 即可 |
| 预览面板内容为空 | 字幕文件加载失败 | 检查文件路径和格式 |
| 术语表为空 | LLM 分析没产生 glossary | 分析提示词已要求至少 5 个术语，换更强模型或降低字幕复杂度 |

### 日志位置

```
%APPDATA%\SubtitleTranslator\logs\translator.log
```

遇到问题时，查看日志中的 `[WARNING]` 和 `[ERROR]` 条目。

---

## 打包分发

### 打包为 Windows .exe

项目已内置 `subtitle_translator.spec`（单文件、无控制台、自动收集 `resources/`）：

```bash
pip install pyinstaller
pyinstaller subtitle_translator.spec --noconfirm
```

产物在 `dist/字幕翻译工具.exe`（约 74MB，PyInstaller 6.18 实测通过；加入 faster-whisper 后体积增至约 180MB，识别模型仍在运行时按需下载、不打进 exe）。

### 需要包含的资源文件

`resources/` 下的 `corpus_vtuber.json`、`glossary_vtuber.json` 和 `styles/` 由 spec 的 `datas` 自动收集。代码通过 `Path(__file__).parent.parent / "resources"` 定位，在打包环境中自动解析到 `sys._MEIPASS/resources`，无需额外处理。

> 若自行用命令行打包，记得加 `--add-data "resources;resources"`。

---

## 常见问题

<details>
<summary><b>Q: 分析完成后为什么翻译不继续？</b></summary>

这是**设计如此**——分析完成后翻译会暂停，让你审核修改分析结果。请查看左侧面板，修改无误后点击「确认分析，继续翻译 →」。如果不需要分析，点「跳过分析，直接翻译」。
</details>

<details>
<summary><b>Q: DeepSeek 的端点到底要不要 /v1？</b></summary>

**不要**。DeepSeek 最新 API 端点为 `https://api.deepseek.com`，不带 `/v1`。如果你的旧脚本用了 `/v1`，也能用，但官方文档推荐不带。
</details>

<details>
<summary><b>Q: 翻译出来的中文还是有点怪？</b></summary>

按以下顺序排查：
1. 术语库配了吗？——「スパチャ→SC」「同接→同接」这种要加到语料库
2. 分析结果改了吗？——风格指南里写清楚翻译偏好
3. 用的什么模型？——DeepSeek V4 Flash 中文效果最好，gpt-4o-mini 有时会「偷懒」
4. 试过术语深度优化吗？——分析面板里的「深度优化术语」按钮
</details>

<details>
<summary><b>Q: 为什么翻译结果里没有标点符号？</b></summary>

**故意设计的**。视频字幕不需要标点——句号、逗号、感叹号在画面上显得杂乱。译文用空格分隔句子，更干净。
</details>

<details>
<summary><b>Q: 可以用本地模型吗（Ollama/vLLM）？</b></summary>

可以。设置中选择「自定义 OpenAI 兼容 API」，端点填 `http://localhost:11434/v1`（Ollama）或你的 vLLM 地址，模型名填你拉取的模型（如 `qwen2.5:7b`）。注意本地模型的上下文窗口通常较小，分析模式选「智能取样」。
</details>

<details>
<summary><b>Q: 术语库和 glossary_vtuber.json 什么关系？</b></summary>

- `corpus_vtuber.json` = 术语库 = 你定义的**强制规则**，优先级最高
- `glossary_vtuber.json` = 预设参考 = 112 条烤肉术语，分析阶段全量注入，翻译阶段只注入本批出现的关键词

你通过 UI 术语库面板编辑的是 `corpus_vtuber.json`。
</details>

<details>
<summary><b>Q: 可以翻译英语字幕吗？</b></summary>

可以。设置 → 语言 → 源语言选「英语」。分析提示词和翻译提示词都有对应的英文版本。
</details>

<details>
<summary><b>Q: 翻译过程中能取消吗？</b></summary>

能。点击底部进度条旁边的「取消」按钮。注意：如果正在 API 调用中，需要等当前调用完成或超时（最多 90 秒）才能真正停止。
</details>

<details>
<summary><b>Q: API Key 安全吗？</b></summary>

Key 在 Windows 上用 **DPAPI（CryptProtectData）** 加密存储在 `%APPDATA%/SubtitleTranslator/keys.enc`。DPAPI 由操作系统托管密钥，绑定**当前 Windows 用户账户**——换机器或换用户都解不开。非 Windows 环境回退到「机器 ID 派生密钥」的 Fernet。这**不是军事级安全**（攻击者以你的用户身份运行程序时，DPAPI 会自动解密），但足以防止：
- 配置文件被直接复制到其他机器/其他用户使用
- 文本编辑器直接看到明文 Key
- 聊天/截图中意外泄露

> 旧版本用明文 `.keyfile` + Fernet 加密，首次启动检测到旧格式会自动迁移到 DPAPI 并删除明文密钥文件；解密失败时会写日志告警（不再静默清空你的密钥）。
</details>

<details>
<summary><b>Q: 提取字幕时提示缺少 faster-whisper？</b></summary>

执行 `pip install faster-whisper`。它只在用到「提取字幕」功能时才被加载，不装也不影响纯字幕翻译。识别模型（whisper 权重）首次提取时才下载，缓存在用户目录，之后离线可用。
</details>

<details>
<summary><b>Q: 能翻译内嵌字幕的视频文件吗？</b></summary>

内嵌**图形**字幕（PGS/VobSub）不能。但可以用「✎ 提取字幕」从视频**音轨**识别出日/英文字幕（SRT），再正常翻译——这就是从视频到熟肉的完整流程。已有的独立字幕文件（SRT/ASS/VTT）直接拖入翻译。
</details>

---

## 贡献指南

欢迎贡献！完整的开发环境搭建、代码规范、测试要求与 PR 流程见 **[CONTRIBUTING.md](CONTRIBUTING.md)**。

参与前请阅读 **[行为准则](CODE_OF_CONDUCT.md)**。

### 快速上手

```bash
git clone https://github.com/balongTTY/subtitle-translator.git
cd subtitle-translator
pip install -e ".[dev]"
pytest                            # 单元测试
python tests/e2e/run_e2e.py       # 端到端验证
python main.py                    # 启动
```

### 贡献方向

| 方向 | 说明 | 难度 |
|------|------|------|
| 🐛 Bug 修复 | 任何导致崩溃、数据丢失、功能异常的 bug | ★★☆ |
| 🌐 新 LLM 适配 | 添加新的 LLM 提供商支持 | ★★☆ |
| 📝 提示词优化 | 改善翻译质量——尤其是翻译腔消除和领域术语 | ★☆☆ |
| 🗂️ 术语库扩充 | 往 `corpus_vtuber.json` 或 `glossary_vtuber.json` 添加更多 vtuber 术语 | ★☆☆ |
| 🎨 UI/UX 改进 | 界面美化、交互优化 | ★★★ |
| 📖 文档完善 | README、代码注释、Wiki | ★☆☆ |
| 🧪 测试 | 单元测试、端到端场景 | ★★☆ |
| 📦 打包脚本 | PyInstaller、CI/CD、自动发布 | ★★★ |

---

## 许可证与致谢

### 许可证

[MIT License](LICENSE) © 2026 balongTTY

版本变更记录见 [CHANGELOG.md](CHANGELOG.md)，详细修复记录见 [docs/fix-records/](docs/fix-records/)。

### 依赖项目

| 项目 | 用途 |
|------|------|
| [pysubs2](https://github.com/tkarabela/pysubs2) | SRT/ASS/VTT 字幕解析与生成 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 字幕提取：whisper 语音识别（CTranslate2 后端，MIT） |
| [PyQt5](https://www.riverbankcomputing.com/software/pyqt/) | 桌面 GUI 框架 |
| [OpenAI Python SDK](https://github.com/openai/openai-python) | OpenAI / DeepSeek / 自定义 API 调用 |
| [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python) | Claude API 调用（含 Prompt Caching） |
| [tiktoken](https://github.com/openai/tiktoken) | Token 数量估算 |
| [cryptography](https://cryptography.io/) | 非 Windows 环境 API Key Fernet 加密兜底 |
| [darkdetect](https://github.com/albertosottile/darkdetect) | Windows 系统明暗主题检测 |

### 术语参考来源

- [萌娘百科 - 虚拟UP主用语与梗](https://moegirl.icu/%E8%99%9A%E6%8B%9FUP%E4%B8%BB%E7%94%A8%E8%AF%AD%E4%B8%8E%E6%A2%97)
- [萌娘百科 - 熟肉](https://mzh.moegirl.org.cn/%E7%86%9F%E8%82%89)
- [B站专栏 - 谈个人2年半烤肉体会](https://www.bilibili.com/read/cv24666246/)
- [B站专栏 - V圈基本用语](https://www.bilibili.com/read/cv21472693/)
- [介绍常见的VTuber术语 & 推荐日翻中频道](https://ayvc0420.github.io/recommendYoutube.html)
- [Nekomoekissaten-Subs 字幕组翻译规范](https://github.com/Nekomoekissaten-SUB/Nekomoekissaten-Subs/wiki/translation)
- [sakuraa.net - 不自然的译文从何而来](https://sakuraa.net/unnatural/)
- [阿改烤肉团公开内训](https://www.bilibili.com/video/BV14nrCYCEWn/)
- [MOJi辞書 - 字幕翻译之趣味](https://www2.mojidict.com/article/6mXN3q6Nns)

---

<p align="center">
  <b>让 AI 烤出观众爱吃的熟肉 🥩</b>
</p>
