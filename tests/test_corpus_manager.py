"""corpus_manager 术语优先级回归测试。

覆盖：
- 语料库（强制）> LLM 分析结果（建议）：split_terms / resolve_conflicts
- 语料库独有词条归入强制；LLM 独有词条归入建议
- 类级缓存跨实例同步（add/remove/update_all 经 _save 落盘并同步）
- get_preset_terms 展平分类结构并缓存

测试用临时语料库文件 + monkeypatch _resolve_corpus_path，隔离真实用户数据；
每个用例重置类级缓存，避免跨用例污染。
"""

import json

import pytest

from core.corpus_manager import CorpusManager


@pytest.fixture
def corpus_file(tmp_path, monkeypatch):
    """生成临时语料库文件，并隔离真实语料库与类级缓存"""
    p = tmp_path / "corpus_vtuber.json"
    p.write_text(
        json.dumps(
            {"version": "1.0", "terms": {"配信開始": "直播開始", "凸待ち": "凸待ち"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    CorpusManager._terms_cache = None
    CorpusManager._preset_cache = None
    monkeypatch.setattr(
        CorpusManager, "_resolve_corpus_path", staticmethod(lambda: p)
    )
    return p


# ---------------------------------------------------------------------------
# 优先级：语料库（强制）> LLM（建议）
# ---------------------------------------------------------------------------

def test_split_terms_corpus_overrides_llm(corpus_file):
    mgr = CorpusManager()
    mandatory, suggested = mgr.split_terms({"配信開始": "放送開始", "新词": "新译"})
    # 语料库已有 → 强制，且用语料库译法覆盖 LLM 译法
    assert mandatory["配信開始"] == "直播開始"
    # LLM 独有 → 建议
    assert suggested == {"新词": "新译"}
    # 语料库中 LLM 未给出的也归入强制
    assert "凸待ち" in mandatory


def test_resolve_conflicts_corpus_wins(corpus_file):
    mgr = CorpusManager()
    merged = mgr.resolve_conflicts({"配信開始": "放送開始", "VOD": "录播"})
    assert merged["配信開始"] == "直播開始"   # 语料库版本覆盖
    assert merged["VOD"] == "录播"            # 语料库没有 → 保留 LLM 版本


# ---------------------------------------------------------------------------
# 读写与类级缓存
# ---------------------------------------------------------------------------

def test_add_then_new_instance_sees_term(corpus_file):
    a = CorpusManager()
    a.add("スパチャ", "SC")
    b = CorpusManager()  # 类级缓存同步 → 新实例立即可见
    assert b.get("スパチャ") == "SC"


def test_remove_syncs_cache(corpus_file):
    a = CorpusManager()
    a.remove("配信開始")
    b = CorpusManager()
    assert b.get("配信開始") is None
    assert "凸待ち" in b.get_all()


def test_update_all_replaces(corpus_file):
    a = CorpusManager()
    a.update_all({"仅剩": "唯一"})
    b = CorpusManager()
    assert b.get_all() == {"仅剩": "唯一"}


def test_add_ignores_blank(corpus_file):
    mgr = CorpusManager()
    mgr.add("   ", "  ")
    assert mgr.get_all() == {"配信開始": "直播開始", "凸待ち": "凸待ち"}


# ---------------------------------------------------------------------------
# 预设参考术语
# ---------------------------------------------------------------------------

def test_get_preset_terms_flattened_and_cached(corpus_file):
    p1 = CorpusManager.get_preset_terms()
    p2 = CorpusManager.get_preset_terms()
    assert p1 == p2
    assert isinstance(p1, dict) and len(p1) > 0
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in p1.items())


class TestConcurrentSave:
    def test_concurrent_save_unique_tmp(self, tmp_path, monkeypatch):
        """并发 _save 使用独立临时文件：不再互相截断导致 os.replace 写入半截 JSON"""
        import threading
        import json as _json
        import core.corpus_manager as CM
        from core.corpus_manager import CorpusManager

        corpus_path = tmp_path / "corpus_test.json"
        monkeypatch.setattr(CorpusManager, "_resolve_corpus_path",
                            staticmethod(lambda: corpus_path))
        saved_cache = CorpusManager._terms_cache
        CorpusManager._terms_cache = None
        try:
            m = CorpusManager()
            errors = []

            def writer(idx):
                try:
                    for k in range(20):
                        m.add(f"术语{idx}_{k}", f"译{idx}_{k}")
                except Exception as e:  # 并发冲突若回归会在这里暴露
                    errors.append(e)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert not errors, errors
            data = _json.loads(corpus_path.read_text(encoding="utf-8"))
            assert len(data["terms"]) == 80  # 4 线程 × 20 条全部落盘
            assert not list(tmp_path.glob("*.tmp")), "临时文件不应残留"
        finally:
            CorpusManager._terms_cache = saved_cache
