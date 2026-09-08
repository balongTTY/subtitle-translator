"""全篇分析器 — 翻译流水线第一阶段"""

import json
import logging
import re

from config.settings import AppSettings
from core.translation_batch import SubtitleEntry, AnalysisResult
from utils.token_counter import estimate_tokens

log = logging.getLogger("subtitle_translator")

LANG_NAMES = {"ja": "日语", "en": "英语", "zh-CN": "简体中文", "zh-TW": "繁体中文"}


def detect_source_lang(entries: list[SubtitleEntry]) -> str:
    """按字幕内容检测源语言：含日文假名 → ja，否则 en

    供「源语言=自动检测」时对每个文件单独判定。
    """
    text = "".join(e.text for e in entries)
    kana = len(re.findall(r"[ぁ-んァ-ヶ]", text))
    return "ja" if kana > 0 else "en"

# 日→中 分析专用提示词
ANALYSIS_PROMPT_JA = """你是一位专门给 vtuber/游戏直播字幕做「烤肉」的翻译顾问。你的任务是在正式翻译之前，先通读整份字幕，产出一份分析报告。这份报告会被注入到后续每一条字幕的翻译提示词里，所以质量直接影响整场翻译的一致性。

## 输入格式
每行格式为 `[原始索引] [MM:SS] 原文`。编号是定位锚点，时间戳帮你判断话题切换。

---

## 思考步骤（先想再写）

在输出 JSON 之前，请按以下顺序思考：

### 第 1 步：扫一遍整体结构
快速浏览全文，回答自己：
- 这场直播大致分几个阶段？（例：开场闲聊 → 游戏实况 → 观众互动 → 结尾道别）
- 每个阶段大约对应哪些索引范围？
- 有没有明显的时间间隔（>5 秒）标记了阶段切换？

### 第 2 步：识别说话特征
- 主播是男的还是女的？（从自称「僕/俺/私/うち」判断）
- 主播语气偏什么风格？（元气/冷静/毒舌/撒娇/池面…）
- 有没有固定的口癖或句尾？（如「〜ですわ」「〜だよね」「〜じゃん」）
- 弹幕/评论被主播读出来时，有没有特定的引述方式？（如「コメントさんが〜」「〇〇さん曰く」）

### 第 3 步：揪出所有需要统一翻译的词
扫一遍全文，把以下类型的词抓出来，逐一给出地道中文翻译：

**直播平台**：配信→直播 / 初配信→出道直播 / 同接→同时在线 / 待機→蹲直播 / 枠→场次 / 歌枠→歌回 / 雑談枠→杂谈回 / 同時視聴→一起看 / アーカイブ→录播 / サムネ→封面 / 概要欄→简介栏 / チャンネル登録→订阅频道 / メンバーシップ→会员 / 投げ銭→打赏

**粉丝互动**：スパチャ→SC/醒目留言 / コメント→弹幕 / 凸待ち→等连麦 / お便り→来信 / お悩み相談→烦恼咨询 / リクエスト→点歌/点播 / コラボ→联动 / 初見→新来的 / 常連→老观众 / マシュマロ→棉花糖

**粉丝文化**：推し→推/本命 / 単推し→单推 / 箱推し→团推 / DD→博爱粉 / ガチ恋→真爱粉 / 認知厨→认知厨 / アンチ→黑粉 / 古参→老粉

**VTuber身份**：中の人→中之人 / 皮→虚拟形象 / デビュー→出道 / 卒業→毕业 / 前世→前世 / 転生→转生 / 個人勢→个人势 / 企業勢→企业势 / 清楚系→清纯路线 / 遜砲→逊炮（自信翻车）

**弹幕/反应**：草→哈哈 / www→www / わかる→懂你 / それな→确实 / 888→鼓掌 / 神→神了 / やばい→糟了/太猛了 / エモい→好感动 / 尊い→太尊了 / すこ→爱了 / てぇてぇ→贴贴

**游戏**：実況→实况 / クリア→通关 / ボス→boss / ラスボス→最终boss / 雑魚→小怪 / 即死→秒杀 / 周回→刷 / 縛りプレイ→限制挑战 / RTA→速通 / レベル上げ→练级

**切片/烤肉**：切り抜き→切片/剪辑 / 生肉→未翻译 / 熟肉→已翻译

**常用口语**：やっぱり→还是/我就说嘛 / ちょっと→有点/稍微 / なるほど→原来如此 / まあ→哎/嗯… / えっと→呃… / さすが→不愧是 / 相変わらず→还是老样子

**其他专有名词**：人名 / 游戏标题 / 角色名 / 地点 / 品牌 / 歌曲名

> 从上述参考清单中选出**本次字幕里确实出现了的**词，再加上字幕里出现的其他专有词。
> 翻译要口语化——「枠」→直播间（不是画框），「凸待ち」→等连麦（不是等待凸起），「スパチャ」→SC/醒目留言（不是超级聊天）。

### 第 4 步：写风格指南
用 2-4 句话描述翻译策略，覆盖：
- 口语化程度（完全口语 vs 半正式）
- 主播性别/语气如何处理
- 敬语策略（です・ます是简化还是保留礼貌感）
- 情绪高扬时（打 boss / 收到スパチャ）的翻译策略

---

## 输出格式（严格 JSON，不要加任何前缀后缀）

```json
{
  "topic_segments": [
    {
      "start_idx": 0,
      "end_idx": 12,
      "time_range": "00:00 ~ 01:23",
      "topic": "开场招呼 + 聊最近在玩的游戏",
      "key_terms": ["配信", "実況"]
    }
  ],
  "speakers": [
    {
      "speaker": "主播",
      "tone": "元气活泼，偶尔吐槽",
      "speech_patterns": ["自称「私」", "句尾多用「〜よ」「〜ね」", "兴奋时连呼「やばい」「すごい」"]
    }
  ],
  "glossary": {
    "配信": "直播",
    "スパチャ": "Super Chat（醒目留言）",
    "コメント": "弹幕",
    "コラボ": "联动",
    "実況": "实况解说"
  },
  "style_guide": "主播语气元气活泼，翻译用口语化中文，句尾语气词（よ/ね/わ）按语境处理，不硬翻。です・ます体简化为自然中文礼貌度。收到スパチャ时的感谢语保持真诚但不夸张。游戏术语用中文玩家圈通用译法。"
}
```

> glossary 至少要给出 5 个词。上面示例只放了 5 个，但你的输出应该根据实际字幕内容给出所有需要统一翻译的词——通常 10-30 个。

{corpus_hint}

{preset_hint}

---

## 字幕内容

{preview}
"""

ANALYSIS_PROMPT_EN = """你是一位专门给直播/视频字幕做翻译的顾问。在正式翻译之前，先通读整份字幕，产出一份分析报告。这份报告会被注入到后续每一条翻译提示词里。

## 输入格式
每行格式为 `[原始索引] [MM:SS] 原文`。

---

## 思考步骤

### 第 1 步：扫一遍整体结构
- 这场直播/视频分几个阶段？（例：intro → main content → Q&A → outro）
- 每个阶段大约对应哪些索引范围？

### 第 2 步：识别说话特征
- 主播/说话人是男是女？语气偏什么风格？（casual / professional / humorous / sarcastic…）
- 有没有固定的口头禅？（like, you know, I mean, literally, chat…）
- 互动方式：自言自语、对观众说话、还是读弹幕/评论？

### 第 3 步：揪出需要统一翻译的词
扫一遍全文，抓出以下类型的词，逐一给出中文翻译：

**平台/直播相关**
stream, livestream, chat, donation, super chat, sub(scribe), membership, VOD, archive, thumbnail, raid, host

**观众互动**
Q&A, AMA, shoutout, call-in, request, suggestion, feedback, comment

**游戏相关**
playthrough, speedrun, boss, final boss, clear, item, equipment, level up, NPC, quest, achievement

**网络用语 / 俚语**
LOL, LMAO, GG, OP, pog, hype, cringe, based, W, ratio, slay, no cap, fr

**专有名词**
人名、游戏名、角色名、品牌、平台名（Twitch/YouTube/Discord）

> 上面是参考清单，从本次字幕里**实际出现了的**词中选，再加上其他专有词。每个词给一句地道的中文翻译。
> glossary 的 value 必须是唯一确定的最终译法：每个词只给一个译法，禁止用「/」「、」「或」罗列备选；圈内约定保留原样的词（草、www、DD），value 直接写原文本身。
> 例：'raid'→「空降（带观众去别的直播间）」；不要机械译成「袭击」

### 第 4 步：写风格指南
2-4 句话描述：口语化程度、俚语脏话处理策略、情绪高扬时的翻译策略。

---

## 输出格式（严格 JSON，不要加任何前缀后缀）

```json
{
  "topic_segments": [
    {
      "start_idx": 0,
      "end_idx": 12,
      "time_range": "00:00 ~ 01:23",
      "topic": "intro + explaining today's plan",
      "key_terms": ["stream", "game"]
    }
  ],
  "speakers": [
    {
      "speaker": "streamer",
      "tone": "casual & energetic",
      "speech_patterns": ["says 'chat' a lot", "uses 'like' as filler"]
    }
  ],
  "glossary": {
    "stream": "直播",
    "chat": "弹幕/观众",
    "donation": "打赏",
    "collab": "联动",
    "sub": "订阅"
  },
  "style_guide": "Casual and colloquial. English slang should find equivalent Chinese internet slang. Donation readings should sound genuinely grateful but not over-the-top. Gaming terms use Chinese player community conventions."
}
```

> glossary 至少 5 个词，通常 10-30 个。

{corpus_hint}

---

## 字幕内容

{preview}
"""


class SubtitleAnalyzer:
    """全篇分析器"""

    # 全文发送占上下文窗口的安全比例（剩余留给系统提示词/术语表/输出）
    FULL_CONTEXT_RATIO = 0.7
    # 智能取样下低于预留预算此比例时直接全文发送
    SMART_FULL_RATIO = 0.8
    # 三段取样的总预算占预留预算的比例（剩余给提示词模板/术语表）
    SAMPLE_BUDGET_RATIO = 0.6
    # 三段预算分配：开头 / 中间 / 结尾
    _HEAD_SHARE, _MID_SHARE = 0.25, 0.10

    def __init__(self) -> None:
        self.settings = AppSettings()

    def build_analysis_prompt(
        self,
        entries: list[SubtitleEntry],
        source_lang: str,
        target_lang: str,
    ) -> str:
        token_limit = self.settings.analysis_tokens
        preview = self._build_preview(entries, token_limit)

        # 加载语料库，作为已知术语告知 LLM
        from core.corpus_manager import CorpusManager
        corpus = CorpusManager()
        corpus_terms = corpus.get_all()
        corpus_hint = ""
        if corpus_terms:
            if len(corpus_terms) > 60:
                log.warning("语料库术语超量：仅向分析提示词注入前 60/%d 条", len(corpus_terms))
            lines = [f"- {k} → {v}" for k, v in list(corpus_terms.items())[:60]]
            corpus_hint = (
                "\n\n## 已知术语（语料库中已收录，无需重复输出）\n"
                "以下术语已有固定翻译，请直接在你的 glossary 中使用这些翻译，不要自创：\n"
                + "\n".join(lines)
            )

        # 预设参考术语（烤肉圈约定译法），仅日文分析时注入
        preset_hint = ""
        if source_lang == "ja":
            preset_terms = corpus.get_preset_terms()
            if preset_terms:
                if len(preset_terms) > 120:
                    log.warning("预设参考术语超量：仅注入前 120/%d 条", len(preset_terms))
                lines = [f"- {k} → {v}" for k, v in list(preset_terms.items())[:120]]
                preset_hint = (
                    "\n\n## 预设参考术语（烤肉圈约定译法）\n"
                    "以下为圈内常用术语的约定译法基准。字幕里用到这些词时，"
                    "请优先参考此表，并在你的 glossary 中采用：\n"
                    + "\n".join(lines)
                )

        if source_lang == "ja":
            return (
                ANALYSIS_PROMPT_JA.replace("{preview}", preview)
                .replace("{corpus_hint}", corpus_hint)
                .replace("{preset_hint}", preset_hint)
            )
        else:
            return (
                ANALYSIS_PROMPT_EN.replace("{preview}", preview)
                .replace("{corpus_hint}", corpus_hint)
                .replace("{preset_hint}", "")
            )

    def _build_preview(self, entries: list[SubtitleEntry], token_limit: int) -> str:
        """
        构建字幕预览。
        - 全文模式（analysis_mode="full"）→ 强制全文发送（受 context_limit 约束）
        - 智能取样（analysis_mode="smart"）→ 短则全文，长则三段连续块

        取样按 token 预算截断（旧实现按条目数百分比切分、无 token 上限：
        总量 20 万 token / 上下文 6.4 万时取样仍约 12 万，请求必然超限——
        最需要分析的超长文件反而系统性拿不到分析结果）。
        """
        model = self.settings.model
        estimated_tokens = sum(estimate_tokens(e.text, model) for e in entries)

        if self.settings.analysis_mode == "full":
            # 全文放不进上下文窗口时回退智能取样，避免分析请求超限
            if estimated_tokens <= self.settings.context_limit * self.FULL_CONTEXT_RATIO:
                return self._format_entries(entries)

        # 智能取样：低于预留预算则全文发送
        if estimated_tokens <= token_limit * self.SMART_FULL_RATIO:
            return self._format_entries(entries)

        # 三段连续取样：预算按比例分配，各段累加条目直到预算耗尽
        budget = int(token_limit * self.SAMPLE_BUDGET_RATIO)
        head_budget = int(budget * self._HEAD_SHARE)
        mid_budget = int(budget * self._MID_SHARE)
        tail_budget = max(budget - head_budget - mid_budget, head_budget)

        n = len(entries)
        head = self._take_within_budget(entries, head_budget, model)
        mid_pool = entries[int(n * 0.45):int(n * 0.75)]
        mid = self._take_within_budget(mid_pool, mid_budget, model)
        tail = self._take_within_budget(entries, tail_budget, model, from_end=True)

        parts = [self._format_entries(head, "── 开头 ──")]
        if mid:
            parts.append(self._format_entries(mid, "── 中间 ──"))
        parts.append(self._format_entries(tail, "── 结尾 ──"))

        sampled = sum(estimate_tokens(e.text, model) for e in head + mid + tail)
        log.info(
            "智能取样：全文约 %d token，取样 %d 条约 %d token（预算 %d）",
            estimated_tokens, len(head) + len(mid) + len(tail), sampled, budget,
        )
        return "\n\n".join(parts)

    @staticmethod
    def _take_within_budget(
        seq: list[SubtitleEntry], budget: int, model: str, from_end: bool = False,
    ) -> list[SubtitleEntry]:
        """按 token 预算取连续条目（from_end=True 从尾部取）。至少保留 1 条，
        防止预算极小时产出空段。"""
        if budget <= 0:
            budget = 1
        order = reversed(seq) if from_end else seq
        picked: list[SubtitleEntry] = []
        used = 0
        for e in order:
            t = estimate_tokens(e.text, model)
            if picked and used + t > budget:
                break
            picked.append(e)
            used += t
        return list(reversed(picked)) if from_end else picked

    def _format_entries(self, entries: list[SubtitleEntry], label: str = "") -> str:
        lines = []
        if label:
            lines.append(label)
        for e in entries:
            start_sec = e.start / 1000
            m = int(start_sec // 60)
            s = int(start_sec % 60)
            lines.append(f"[{e.index}] [{m:02d}:{s:02d}] {e.text}")
        return "\n".join(lines)

    def parse_analysis_response(self, response_text: str) -> AnalysisResult:
        result = AnalysisResult(raw_response=response_text)
        parsed_ok = False
        try:
            json_str = self._extract_json(response_text)
            if not json_str:
                log.warning("未能从 LLM 响应中提取 JSON")
                raise ValueError("未能从 LLM 响应中提取 JSON")

            data = self._loads_lenient(json_str)

            # 规范化 topic_segments：单条坏字段（非 dict / 非数字索引）只跳过该条，
            # 避免一条坏数据把整篇分析结果作废
            normalized_segments: list[dict] = []
            for seg in data.get("topic_segments", []):
                try:
                    if not isinstance(seg, dict):
                        continue
                    seg.setdefault("time_range", "")
                    seg.setdefault("key_terms", [])
                    # 确保索引是整数（"0-5"/"abc" 等非数字串会抛 ValueError，跳过该分段）
                    seg["start_idx"] = int(seg.get("start_idx", 0))
                    seg["end_idx"] = int(seg.get("end_idx", 0))
                except (ValueError, AttributeError, TypeError) as e:
                    log.warning("跳过无效的话题分段: %s", e)
                    continue
                normalized_segments.append(seg)

            result.topic_segments = normalized_segments
            result.speakers = data.get("speakers", [])

            # glossary 可能是 list 或 dict
            glossary_raw = data.get("glossary", {})
            if isinstance(glossary_raw, list):
                result.glossary = {}
                for item in glossary_raw:
                    if isinstance(item, dict):
                        src = item.get("source") or item.get("src") or item.get("term") or ""
                        tgt = item.get("target") or item.get("tgt") or item.get("translation") or ""
                        if src and tgt:
                            result.glossary[src] = tgt
            elif isinstance(glossary_raw, dict):
                result.glossary = {str(k): str(v) for k, v in glossary_raw.items() if k and v}
            else:
                result.glossary = {}

            result.style_guide = data.get("style_guide", "")

            parsed_ok = True

        except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as e:
            log.warning("解析分析结果失败: %s", e)

        # 解析失败（无 JSON/坏 JSON）必须显式失败而不是返回空对象：
        # 空对象会让 translator 走分析确认流程，用户面对空话题/空术语面板
        # 极易直接确认，整篇翻译在没有上下文与术语的情况下静默进行。
        # 抛错则由 translator 的 except 分支接管（重试一次后跳过分析直接翻译）。
        # 注意与「解析成功但结构确实为空」区分——短字幕没有话题/术语是合法的，
        # 不能拒绝，否则正常的空分析会被误判成失败
        if not parsed_ok:
            raise ValueError(
                f"分析响应解析失败（响应长度 {len(response_text or '')}），"
                "无法获得话题/术语结构，将跳过全篇分析直接翻译"
            )

        return result

    # ------------------------------------------------------------------
    #  术语深度优化
    # ------------------------------------------------------------------

    GLOSSARY_OPTIMIZE_PROMPT = """你是 vtuber/直播字幕的术语翻译专家。现在要对下面这份术语表做深度优化。

## 当前术语表

{glossary_text}

## 参考上下文（字幕抽样）

{preview}

## 优化任务

逐条审视每个术语，从以下角度检查：

### 1. 翻译自然度
- 这个中文翻译是**真人口语**还是**字典味机翻**？
- vtuber 粉丝圈实际会用这个说法吗？
- 例：配信→「直播」OK；配信→「分发」NG

### 2. 游戏/直播领域准确性
- 游戏术语用了中文玩家圈的约定译法吗？
- 直播平台术语对应中文圈的常用说法吗？
- 例：スパチャ→「Super Chat（醒目留言）」OK；スパチャ→「超级聊天」NG
- 例：コラボ→「联动」OK；コラボ→「协作」NG
- 例：切り抜き→「切片/剪辑」OK；切り抜き→「剪下」NG

### 3. 缺失检查
- 扫一眼参考上下文，有没有**出现但未收录**的重要术语？
- 人名、游戏名、专有名词是否都收了？

### 4. 冗余清理
- 是否收录了不需要翻译的通用词？（如「はい」「そうです」）
- 是否重复收录了同义词的不同写法？

---

## 返回格式（只返回 JSON，不要任何解释）

```json
{
  "glossary": {
    "配信": "直播",
    "スパチャ": "Super Chat（醒目留言）"
  },
  "changes": "用一句话总结做了哪些修改（如：优化了5个翻译，新增了3个术语，删除了1个冗余词）"
}
```

直接输出优化后的完整 glossary，不需要保留旧的。"""

    def build_glossary_optimize_prompt(
        self,
        glossary: dict[str, str],
        entries: list[SubtitleEntry],
    ) -> str:
        """构建术语优化提示词"""
        # 术语表文本
        gloss_lines = [f"- {k} → {v}" for k, v in glossary.items()]
        glossary_text = "\n".join(gloss_lines) if gloss_lines else "（空）"

        # 字幕抽样
        preview = self._build_preview(entries, token_limit=8000)

        return self.GLOSSARY_OPTIMIZE_PROMPT.replace(
            "{glossary_text}", glossary_text
        ).replace("{preview}", preview)

    def parse_glossary_optimize_response(self, response_text: str) -> dict[str, str]:
        """解析优化后的术语表"""
        json_str = self._extract_json(response_text)
        if not json_str:
            return {}
        try:
            data = self._loads_lenient(json_str)
            glossary = data.get("glossary", {})
            if isinstance(glossary, dict):
                return {str(k): str(v) for k, v in glossary.items() if k and v}
        except Exception:
            pass
        return {}

    def _extract_json(self, text: str) -> str | None:
        # 尝试提取 ```json ... ``` 块（返回原文，不做清理——
        # 盲目替换 ", }" 会破坏字符串值里合法出现的该序列）
        if "```json" in text:
            start = text.find("```json") + 7
            end = text.find("```", start)
            if end > start:
                return text[start:end].strip()
            # 缺少闭合 ``` → 从 ```json 后取到末尾
            return text[start:].strip()

        if "```" in text:
            start = text.find("```") + 3
            end = text.find("```", start)
            if end > start:
                return text[start:end].strip()
            return text[start:].strip()

        # 尝试找到第一个 { 和最后一个 }
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            return text[brace_start:brace_end + 1]

        return None

    @staticmethod
    def _loads_lenient(s: str) -> dict:
        """先按原文解析；JSONDecodeError 才做尾逗号清理重试。

        直接对原文做 r",\\s*([}\\]])" 替换是纯文本操作——字符串值里合法的
        「逗号+右括号」序列（如 glossary 译文末尾）会被改写且恰好解析成功，
        内容静默损坏。
        """
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return json.loads(SubtitleAnalyzer._clean_json(s))

    @staticmethod
    def _clean_json(s: str) -> str:
        """清理 LLM JSON 输出的常见瑕疵：末尾逗号（仅在直接解析失败后使用）"""
        # 去掉 }/] 前多余的逗号（不处理字符串内的逗号——此正则只匹配"前有值的逗号"）
        cleaned = re.sub(r",\s*([}\]])", r"\1", s)
        return cleaned
