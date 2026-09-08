"""R2 复核：完成计数正确性 + 取消竞态

验证两点：
1. 失败文件被正确计为 failed（results 里为空串标记，成功/失败计数正确）
2. 取消后不再发出 file_completed / all_completed 等迟到完成信号
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(os.path.abspath(__file__)).parent.parent.parent))

from PyQt5.QtWidgets import QApplication
import core.translator as T
from config.settings import AppSettings

# 模块级常驻 QApplication——若作为局部变量，函数结束时被 GC，
# 连带销毁单例 AppSettings 持有的 QSettings，导致后续访问崩溃
_APP = QApplication(sys.argv)


class BlockingLLM:
    """translate_batch 可阻塞并通知，模拟真实 API 调用"""

    def __init__(self, entered: threading.Event | None = None, block_s: float = 3.0):
        self.entered = entered
        self.block_s = block_s

    def analyze(self, prompt):
        return '{"topic_segments": [], "glossary": {}, "style_guide": ""}'

    def translate_batch(self, batch, src, tgt):
        if self.entered:
            self.entered.set()
        if self.block_s:
            time.sleep(self.block_s)
        return ["译文" for _ in range(batch.primary_end_idx - batch.primary_start_idx)]

    def validate_api_key(self):
        return True


def make_srt(path: Path) -> None:
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n", encoding="utf-8")


def test_counting() -> None:
    s = AppSettings()
    s.parallel_files = 2
    T.TranslatorWorker._get_llm_service = lambda self: BlockingLLM(block_s=0)

    tmp = Path(tempfile.mkdtemp())
    good = tmp / "good.srt"
    make_srt(good)
    missing = tmp / "missing.srt"  # 不存在 → 加载抛异常 → 计为失败

    facade = T.TranslatorFacade()
    facade.prepare_translation([str(good), str(missing)])
    emitted = []
    facade.signals.all_completed.connect(lambda r: emitted.append(r))
    facade.signals.waiting_for_approval.connect(lambda fp: facade.approve_analysis(fp, None))
    facade.start()

    deadline = time.time() + 20
    while not emitted and time.time() < deadline:
        _APP.processEvents()
        time.sleep(0.02)
    _APP.processEvents()

    assert emitted, "all_completed 未触发"
    results = emitted[0]
    assert results[str(good)], "正常文件应成功"
    assert results[str(missing)] == "", "失败文件应为空串标记"
    success = sum(1 for v in results.values() if v)
    failed = len(results) - success
    print(f"[计数] 成功={success}, 失败={failed}")
    assert success == 1 and failed == 1, f"失败文件未计入 failed: {results}"
    facade._force_cleanup()
    print("[计数] ✓ 失败文件正确计为 failed")


def test_cancel_race() -> None:
    s = AppSettings()
    s.parallel_files = 1
    entered = threading.Event()
    T.TranslatorWorker._get_llm_service = lambda self: BlockingLLM(entered=entered, block_s=3.0)

    tmp = Path(tempfile.mkdtemp())
    f = tmp / "a.srt"
    make_srt(f)

    facade = T.TranslatorFacade()
    facade.prepare_translation([str(f)])
    late_file_completed = []
    late_all_completed = []
    facade.signals.file_completed.connect(
        lambda fp, out: late_file_completed.append(fp)
    )
    facade.signals.all_completed.connect(lambda r: late_all_completed.append(r))
    facade.signals.waiting_for_approval.connect(lambda fp: facade.approve_analysis(fp, None))
    facade.start()

    # 等批次真正进入 translate_batch（在慢 LLM 里 sleep）。
    # 注意必须边 processEvents 边轮询——信号是排队投递，阻塞等待会饿死确认流程
    deadline = time.time() + 10
    while not entered.is_set() and time.time() < deadline:
        _APP.processEvents()
        time.sleep(0.02)
    assert entered.is_set(), "批次未进入 translate_batch"
    # 批次进行中取消
    facade.cancel()
    t0 = time.time()
    deadline = time.time() + 8  # 等慢 LLM 的 sleep 结束、worker 走到取消检查
    while time.time() < deadline:
        _APP.processEvents()
        time.sleep(0.02)
    elapsed = time.time() - t0

    print(f"[取消] 取消后 {elapsed:.1f}s 内 file_completed={len(late_file_completed)} 次, "
          f"all_completed={len(late_all_completed)} 次")
    assert not late_file_completed, "取消后不应再发 file_completed"
    assert not late_all_completed, "取消后不应再发 all_completed"
    # worker 线程应最终退出（无泄漏）
    assert not any(t.isRunning() for t in facade._threads), "取消后线程应退出"
    print("[取消] ✓ 取消后无迟到完成信号，线程正常退出")


def main() -> int:
    test_counting()
    test_cancel_race()
    print("\nR2 复核测试全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
