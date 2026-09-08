"""语料库（强制术语表）管理器"""

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from utils.constants import APP_ORG

log = logging.getLogger("subtitle_translator")

# 打包（PyInstaller 单文件）环境下打包目录内的初始语料库文件名
_CORPUS_FILENAME = "corpus_vtuber.json"


class CorpusManager:
    """管理用户定义的强制术语表，优先级高于 LLM 分析结果"""

    _preset_cache: dict[str, str] | None = None
    # 类级缓存：语料库内容跨实例复用（analyzer/prompt_builder 每批新建实例）。
    # 所有写路径（add/remove/update_all）都经 _save() 落盘并同步缓存。
    _terms_cache: dict[str, str] | None = None

    def __init__(self) -> None:
        self._corpus_path = self._resolve_corpus_path()
        self._terms: dict[str, str] = {}
        self._load()

    @staticmethod
    def _resolve_corpus_path() -> Path:
        """定位语料库文件路径。

        打包（PyInstaller 单文件）环境下，`__file__` 指向只读的 _MEIPASS
        临时目录，写入会在重启后被丢弃；改为写入用户数据目录
        （%APPDATA%/SubtitleTranslator/），首次运行把打包内初始 json 复制过去。
        """
        if getattr(sys, "frozen", False):
            user_dir = Path(
                os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")
            ) / APP_ORG
            user_dir.mkdir(parents=True, exist_ok=True)
            target = user_dir / _CORPUS_FILENAME
            if not target.exists():
                # 首次运行：从打包目录复制初始语料库
                bundled = (
                    Path(__file__).parent.parent / "resources" / _CORPUS_FILENAME
                )
                if bundled.exists():
                    try:
                        target.write_bytes(bundled.read_bytes())
                    except OSError as e:
                        log.warning("复制初始语料库失败: %s", e)
            return target
        return Path(__file__).parent.parent / "resources" / _CORPUS_FILENAME

    def _load(self) -> None:
        if CorpusManager._terms_cache is not None:
            self._terms = dict(CorpusManager._terms_cache)
            return
        if self._corpus_path.exists():
            try:
                data = json.loads(self._corpus_path.read_text(encoding="utf-8"))
                self._terms = dict(data.get("terms", {}))
            except Exception as e:
                # 解析失败不能把空表写进类级缓存——那会"投毒"：之后所有新实例
                # （含 GUI）都拿到空语料库，用户的强制术语静默消失，直到下次写盘。
                # 保留缓存为 None，让下一个实例重新尝试读盘
                log.warning("加载语料库失败: %s", e)
                self._terms = {}
                return
        CorpusManager._terms_cache = dict(self._terms)

    def _save(self) -> None:
        data = {"version": "1.0", "terms": self._terms}
        self._corpus_path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：翻译进行中 GUI 改语料库时，工作线程可能正按 mtime 重建实例
        # 读盘——write_text 直接写会被读到半截 JSON，触发解析失败。
        # 临时文件名带 pid+线程 id：固定名会让并发 _save 互相截断——
        # os.replace 可把半截 JSON 换名成正式文件，语料库损坏且静默清空
        tmp = self._corpus_path.with_name(
            f"{self._corpus_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            # Windows 下两个 os.replace 并发飞时目标会被瞬时锁住抛
            # PermissionError——短暂重试即可，属正常竞争而非权限问题
            for attempt in range(5):
                try:
                    os.replace(tmp, self._corpus_path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        # 同步类级缓存，保证后续新建实例读到最新数据
        CorpusManager._terms_cache = dict(self._terms)

    # ---- 存取 ----

    def get_all(self) -> dict[str, str]:
        return dict(self._terms)

    def get(self, term: str) -> str | None:
        return self._terms.get(term)

    def add(self, term: str, translation: str) -> None:
        term = term.strip()
        translation = translation.strip()
        if term and translation:
            self._terms[term] = translation
            self._save()

    def remove(self, term: str) -> None:
        if term in self._terms:
            del self._terms[term]
            self._save()

    def update_all(self, terms: dict[str, str]) -> None:
        """批量替换术语表"""
        self._terms = {k.strip(): v.strip() for k, v in terms.items() if k and v}
        self._save()

    # ---- 预设参考术语（glossary_vtuber.json） ----

    @staticmethod
    def get_preset_terms() -> dict[str, str]:
        """加载烤肉圈预设术语表（分类结构 → 扁平 dict）。

        这份是「参考基准」：告诉 LLM 圈内约定译法，但不强制（强制项在语料库）。
        按需调用，每次读文件 + 缓存。
        """
        if CorpusManager._preset_cache is not None:
            return dict(CorpusManager._preset_cache)

        preset_path = Path(__file__).parent.parent / "resources" / "glossary_vtuber.json"
        flat: dict[str, str] = {}
        try:
            data = json.loads(preset_path.read_text(encoding="utf-8"))
            for value in data.values():
                if isinstance(value, dict):
                    for k, v in value.items():
                        if isinstance(k, str) and isinstance(v, str) and k and v:
                            flat[k] = v
        except Exception as e:
            log.warning("加载预设术语表失败: %s", e)

        CorpusManager._preset_cache = flat
        return dict(flat)

    # ---- 冲突解决 ----

    def resolve_conflicts(self, llm_glossary: dict[str, str]) -> dict[str, str]:
        """
        合并 LLM 生成的 glossary 与语料库：
        - 语料库中已有的术语 → 使用语料库版本（强制）
        - 语料库中没有的 → 使用 LLM 版本（建议）
        返回合并后的 glossary。
        """
        merged = dict(llm_glossary)
        for term, translation in self._terms.items():
            merged[term] = translation
        return merged

    def split_terms(self, llm_glossary: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        """
        将术语表拆分为强制（语料库已有）和建议（仅 LLM 给出）两部分。
        返回: (mandatory, suggested)
        """
        mandatory: dict[str, str] = {}
        suggested: dict[str, str] = {}

        for term, translation in llm_glossary.items():
            if term in self._terms:
                mandatory[term] = self._terms[term]
            else:
                suggested[term] = translation

        # 语料库中有但 LLM 没给出的也加入强制
        for term, translation in self._terms.items():
            if term not in mandatory:
                mandatory[term] = translation

        return mandatory, suggested
