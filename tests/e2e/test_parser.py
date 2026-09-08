"""LLM 输出解析鲁棒性测试（parse_response 变体 + JSON 尾逗号清理 + 批量确认）"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(os.path.abspath(__file__)).parent.parent.parent))

from services.prompt_builder import parse_response
from core.translation_batch import TranslationBatch, SubtitleEntry
from core.analyzer import SubtitleAnalyzer


def test_parse_response():
    entries = [SubtitleEntry(i, 0, 0, f"g{i}", "x") for i in range(3)]
    b = TranslationBatch(batch_id=0, entries=entries, primary_start_idx=0, primary_end_idx=3)

    variants = [
        "0. aaa\n1、bbb\n2: ccc",          # 混合分隔符
        "- 0. aaa\n- 1. bbb\n- 2. ccc",    # markdown 列表
        "**0. aaa**\n1. bbb\n2. ccc",      # 星号强调
        "0. aaa\n1. bbb\n2. ccc\n```结尾```",  # 多余尾部
        "0. aaa\n2: ccc",                  # 少数缺条 → 缺口回退原文
    ]
    expect = [
        ["aaa", "bbb", "ccc"],
        ["aaa", "bbb", "ccc"],
        ["aaa", "bbb", "ccc"],
        ["aaa", "bbb", "ccc"],
        ["aaa", "g1", "ccc"],
    ]
    for i, (v, exp) in enumerate(zip(variants, expect)):
        r = parse_response(v, b)
        assert r == exp, f"变体{i} 解析失败: {r}"
    # 大面积缺条（>一半）→ 确定性失败，不再静默产出全原文成品
    from services.base_llm import DeterministicError
    try:
        parse_response("0. aaa", b)
        raise AssertionError("超过半数缺条应抛 DeterministicError")
    except DeterministicError:
        pass
    print(f"parse_response {len(variants)} 种变体解析 ✓")


def test_json_cleanup():
    an = SubtitleAnalyzer()
    import json
    j = '{"glossary": {"a": "1", "b": "2",}, "topic_segments": [{"start_idx": 0,},],}'
    cleaned = an._clean_json(j)
    data = json.loads(cleaned)
    assert data["glossary"] == {"a": "1", "b": "2"}, "尾逗号清理后 glossary 错误"
    assert data["topic_segments"] == [{"start_idx": 0}], "尾逗号清理后 topic_segments 错误"
    # 带围栏的完整响应（_extract_json 只提取原文，尾逗号由 _loads_lenient 容错）
    resp = '```json\n{"glossary": {"x": "y",}}\n```'
    assert an._loads_lenient(an._extract_json(resp)) == {"glossary": {"x": "y"}}, "围栏提取+容错解析失败"
    print("JSON 尾逗号/围栏提取 ✓")


def test_approve_all():
    from PyQt5.QtWidgets import QApplication
    import core.translator as T
    from config.settings import AppSettings

    app = QApplication(sys.argv)
    s = AppSettings()
    s.parallel_files = 3
    T.TranslatorWorker._get_llm_service = lambda self: FakeLLM()

    tmp = Path(tempfile.mkdtemp())
    files = []
    for name in ("a.srt", "b.srt", "c.srt"):
        p = tmp / name
        p.write_text("1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n", encoding="utf-8")
        files.append(str(p))

    facade = T.TranslatorFacade()
    facade.prepare_translation(files)
    done = []
    facade.signals.all_completed.connect(lambda r: done.append(r))
    # 不逐个确认——等三个文件都进入等待后一次"确认全部"
    facade.start()
    deadline = time.time() + 20
    while len(facade._workers) and not all(
        (w.waiting_file is not None) for w in facade._workers
    ) and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    waiting = [w.waiting_file is not None for w in facade._workers]
    print(f"等待确认的 worker: {waiting}")
    assert all(waiting), "三个文件都应进入等待确认"
    # 确认全部
    facade.approve_all()
    deadline = time.time() + 20
    while not done and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    assert done and len(done[0]) == 3, "确认全部后 3 个文件都应完成"
    print("批量「确认全部」→ 3 个文件一次全部完成 ✓")
    facade._force_cleanup()


class FakeLLM:
    def analyze(self, prompt):
        return '{"topic_segments": [], "glossary": {}, "style_guide": ""}'

    def translate_batch(self, batch, src, tgt):
        return ["译文" for _ in range(batch.primary_end_idx - batch.primary_start_idx)]

    def validate_api_key(self):
        return True


def main() -> int:
    test_parse_response()
    test_json_cleanup()
    test_approve_all()
    print("\n解析鲁棒性 + 批量确认测试全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
