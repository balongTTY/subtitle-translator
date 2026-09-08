"""TranslatorWorker 三阶段流水线集成测试（假 LLM，不触网、不起 QThread）

覆盖：
- 完整流水线：加载 → 标签提取 → 跳过项判定 → 分批 → 翻译 → 质检 → 合并 → 落盘
- LLM 调用失败 → 回退原文 + needs_review
- 取消：批次循环中断，不落盘
- 坏文件：file_failed 信号 + all_completed 空串标记
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.translator as translator_mod
from core.translator import TranslatorWorker
from core.subtitle_io import SubtitleFile


SRT_INPUT = (
    "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n\n"
    "2\n00:00:02,500 --> 00:00:03,500\n{\\i1}元気？{\\i0}\n\n"
    "3\n00:00:04,000 --> 00:00:05,000\n♪\n\n"
)

TRANSLATIONS = {
    "こんにちは": "你好",
    "<TAG_0>元気？<TAG_1>": "<TAG_0>状态不错<TAG_1>",
}


class FakeLLM:
    """假 LLM：analyze 必然失败（走「跳过分析」路径），translate 按字典映射"""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.batch_calls: list[list[str]] = []

    def analyze(self, prompt: str) -> str:
        raise RuntimeError("分析不可用")

    def translate_batch(self, batch, source_lang, target_lang):
        primary = batch.entries[batch.primary_start_idx:batch.primary_end_idx]
        self.batch_calls.append([e.text for e in primary])
        if self.fail:
            raise RuntimeError("API down")
        return [TRANSLATIONS.get(e.text, f"译:{e.text}") for e in primary]


class SignalRecorder:
    """按信号名收集发射参数"""

    def __init__(self, worker: TranslatorWorker):
        self.events: dict[str, list] = {}
        for name in ("file_started", "batch_started", "batch_completed",
                     "batch_qc_failed", "file_completed", "file_failed",
                     "all_completed", "log_message"):
            self.events[name] = []
            getattr(worker.signals, name).connect(
                lambda *a, n=name: self.events[n].append(a)
            )


@pytest.fixture
def srt_file(tmp_path) -> Path:
    p = tmp_path / "video.srt"
    p.write_text(SRT_INPUT, encoding="utf-8")
    return p


def _make_worker(monkeypatch, fail: bool = False) -> tuple[TranslatorWorker, FakeLLM]:
    fake = FakeLLM(fail=fail)
    monkeypatch.setattr(
        translator_mod.TranslatorWorker, "_get_llm_service", lambda self: fake,
    )
    worker = translator_mod.TranslatorWorker([])
    return worker, fake


class TestHappyPath:
    def test_full_pipeline(self, monkeypatch, tmp_path, srt_file):
        worker, fake = _make_worker(monkeypatch)
        rec = SignalRecorder(worker)
        worker.filepaths = [str(srt_file)]

        worker.run()

        # 完成信号：file_completed + all_completed（含输出路径）
        assert len(rec.events["file_completed"]) == 1
        out_path = rec.events["file_completed"][0][1]
        assert Path(out_path).name == "video_zh.srt"
        assert rec.events["all_completed"] == [( {str(srt_file): out_path}, )]

        # 输出内容：翻译 + 标签还原 + 跳过项透传
        # （pysubs2 的 .text 是 plaintext（剥 ASS 标签），标签保留在 .original_text）
        reloaded = SubtitleFile().load(out_path)
        assert [e.text for e in reloaded] == ["你好", "状态不错", "♪"]
        assert reloaded[1].original_text == "{\\i1}状态不错{\\i0}"

        # 批次调用：跳过项 ♪ 不进批次
        assert len(fake.batch_calls) == 1
        assert fake.batch_calls[0] == ["こんにちは", "<TAG_0>元気？<TAG_1>"]

        # 无 QC 失败信号
        assert rec.events["batch_qc_failed"] == []

    def test_no_analysis_skip_log(self, monkeypatch, tmp_path, srt_file):
        """analyze 抛异常 → 日志提示跳过，流程继续"""
        worker, _ = _make_worker(monkeypatch)
        rec = SignalRecorder(worker)
        worker.filepaths = [str(srt_file)]
        worker.run()
        logs = " ".join(a[0] for a in rec.events["log_message"])
        assert "全篇分析失败" in logs
        assert "阶段二: 分批翻译 (1 批)" in logs


class TestLLMFailure:
    def test_translate_failure_falls_back_to_original(self, monkeypatch, tmp_path, srt_file):
        worker, _ = _make_worker(monkeypatch, fail=True)
        rec = SignalRecorder(worker)
        worker.filepaths = [str(srt_file)]
        worker.run()

        # QC 失败信号发出（回退原文批次）
        assert len(rec.events["batch_qc_failed"]) == 1
        results = rec.events["batch_qc_failed"][0][2]
        assert all(r.status == "needs_review" for r in results)
        assert any("已回退原文" in i for r in results for i in r.qc_issues)

        # 文件仍落盘（内容为原文），不标记为失败文件
        out_path = rec.events["file_completed"][0][1]
        reloaded = SubtitleFile().load(out_path)
        assert reloaded[0].text == "こんにちは"
        assert reloaded[1].original_text == "{\\i1}元気？{\\i0}"  # 占位符链路完整还原
        assert rec.events["file_failed"] == []


class TestCancel:
    def test_cancel_before_run_processes_nothing(self, monkeypatch, tmp_path, srt_file):
        worker, _ = _make_worker(monkeypatch)
        rec = SignalRecorder(worker)
        worker.filepaths = [str(srt_file)]
        worker.cancel()
        worker.run()

        assert rec.events["file_completed"] == []
        assert rec.events["file_failed"] == []
        assert rec.events["all_completed"] == [({}, )]
        assert not (tmp_path / "video_zh.srt").exists()

    def test_cancel_between_batches(self, monkeypatch, tmp_path):
        """批次循环中检测取消：已译批次保留，未译批次不请求，不落盘"""
        worker, _ = _make_worker(monkeypatch)
        rec = SignalRecorder(worker)

        # 构造 8 条短字幕 → 2 个批次（默认 max_entries=30、target_tokens=2000，
        # 但用极低 context_limit 迫使分批）
        lines = []
        for i in range(8):
            lines.append(f"{i + 1}\n00:00:0{i},000 --> 00:00:0{i},500\n字幕内容第{i}条\n\n")
        src = tmp_path / "many.srt"
        src.write_text("".join(lines), encoding="utf-8")

        fake = FakeLLM()

        from config.settings import AppSettings
        s = AppSettings()
        saved_ctx = s.context_limit
        # max_tokens = min(3000, ctx//2)；ctx=1000 → 单批最多 500 token ≈ 数条
        s.context_limit = 1000
        try:
            def translate_with_cancel(batch, source, target):
                worker2.cancel()  # 第一个批次翻译后取消（取消的是实际运行的 worker2）
                primary = batch.entries[batch.primary_start_idx:batch.primary_end_idx]
                return [f"译{e.index}" for e in primary]

            fake.translate_batch = translate_with_cancel
            monkeypatch.setattr(
                translator_mod.TranslatorWorker, "_get_llm_service", lambda self: fake,
                raising=False,
            )
            worker2 = translator_mod.TranslatorWorker([str(src)])
            rec2 = SignalRecorder(worker2)
            worker2.run()
        finally:
            s.context_limit = saved_ctx

        # 取消后不落盘、不上报完成（该文件以空串标记出现在 all_completed 中）
        assert rec2.events["file_completed"] == []
        assert rec2.events["all_completed"] == [({str(src): ""}, )]
        assert not (tmp_path / "many_zh.srt").exists()


class TestBadFile:
    def test_unloadable_file_reports_failure(self, monkeypatch, tmp_path):
        worker, _ = _make_worker(monkeypatch)
        rec = SignalRecorder(worker)
        bad = tmp_path / "broken.srt"
        bad.write_bytes(b"\x00\x01\x02 garbage")
        worker.filepaths = [str(bad)]
        worker.run()

        assert len(rec.events["file_failed"]) == 1
        fp, msg = rec.events["file_failed"][0]
        assert fp == str(bad)
        assert "无法识别" in msg
        # all_completed 用空串标记失败文件
        assert rec.events["all_completed"] == [({str(bad): ""}, )]
        assert not (tmp_path / "broken_zh.srt").exists()


class TestQcFailedDowngrade:
    def test_passable_batch_downgrades_failed_entries(self, monkeypatch, tmp_path):
        """20% 阈值放行的批次里，qc_failed 条目降级 needs_review 并标注，
        不再以「成功译文」形态静默落盘"""
        # 6 条字幕，1 条译文与原文相同（严重问题 1/6 ≈ 17% ≤ 20% → 批次放行）
        lines = []
        texts_ja = ["おはよう", "ありがとう", "すみません", "がんばって", "またね", "さようなら"]
        for i, t in enumerate(texts_ja):
            lines.append(f"{i + 1}\n00:00:0{i},000 --> 00:00:0{i},900\n{t}\n\n")
        src = tmp_path / "six.srt"
        src.write_text("".join(lines), encoding="utf-8")

        translations = {
            "おはよう": "早上好", "ありがとう": "谢谢", "すみません": "不好意思",
            "がんばって": "加油", "またね": "回头见",
            "さようなら": "さようなら",  # 整句未翻译 → 严重问题
        }

        class _LLM(FakeLLM):
            def translate_batch(self, batch, source_lang, target_lang):
                primary = batch.entries[batch.primary_start_idx:batch.primary_end_idx]
                self.batch_calls.append([e.text for e in primary])
                return [translations.get(e.text, f"译:{e.text}") for e in primary]

        fake = _LLM()
        monkeypatch.setattr(
            translator_mod.TranslatorWorker, "_get_llm_service", lambda self: fake,
        )
        worker = translator_mod.TranslatorWorker([str(src)])
        rec = SignalRecorder(worker)
        worker.run()

        assert len(rec.events["batch_completed"]) == 1
        results = rec.events["batch_completed"][0][2]
        by_text = {r.original_text: r for r in results}
        failed = by_text["さようなら"]
        assert failed.status == "needs_review"
        assert "批次放行，但本条存在未解决质检问题" in failed.qc_issues
        assert sum(1 for r in results if r.status == "qc_passed") == 5


class TestRetryCancel:
    def test_cancel_during_retry_stops_immediately(self, monkeypatch, tmp_path, srt_file):
        """取消后批次重试循环立即终止，不再发起后续 API 调用"""
        calls = {"n": 0}

        class _LLM(FakeLLM):
            def translate_batch(self, batch, source_lang, target_lang):
                calls["n"] += 1
                worker.cancel()  # 首次失败即取消
                raise RuntimeError("API down")

        fake = _LLM()
        monkeypatch.setattr(
            translator_mod.TranslatorWorker, "_get_llm_service", lambda self: fake,
        )
        worker = translator_mod.TranslatorWorker([str(srt_file)])
        worker.run()
        assert calls["n"] == 1  # 未取消时默认会重试 max_qc_retries 次


class TestApprovalTimeoutFacade:
    def test_timeout_advances_approval_queue(self, monkeypatch):
        """worker 确认超时后 facade 必须推进队列，否则后续文件面板永不展示"""
        facade = translator_mod.TranslatorFacade()
        w1 = translator_mod.TranslatorWorker([])
        w2 = translator_mod.TranslatorWorker([])
        facade._workers = [w1, w2]

        facade._pending_analysis["a.srt"] = None
        facade._on_worker_waiting("a.srt")     # 展示 a
        facade._on_worker_waiting("b.srt")     # b 排队
        assert facade._showing_approval == "a.srt"

        w1.waiting_file = "a.srt"
        w2.waiting_file = "b.srt"
        facade._on_worker_approval_timed_out("a.srt")  # a 超时

        assert facade._showing_approval == "b.srt"     # b 被推上展示位
        assert "a.srt" not in facade._pending_analysis

    def test_timeout_without_queue_clears_state(self, monkeypatch):
        facade = translator_mod.TranslatorFacade()
        facade._pending_analysis["a.srt"] = None
        facade._on_worker_waiting("a.srt")
        facade._on_worker_approval_timed_out("a.srt")
        assert facade._showing_approval is None
