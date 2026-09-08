"""P1 并行翻译端到端测试（使用 fake LLM，不触网）"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(os.path.abspath(__file__)).parent.parent.parent))

from PyQt5.QtWidgets import QApplication
import core.translator as T
from config.settings import AppSettings


class FakeLLM:
    def analyze(self, prompt):
        return '{"topic_segments": [], "glossary": {"スパチャ": "SC"}, "style_guide": ""}'

    def translate_batch(self, batch, src, tgt):
        return ["你好啊" for _ in range(batch.primary_end_idx - batch.primary_start_idx)]

    def validate_api_key(self):
        return True


def make_srt(path: Path, n: int) -> None:
    lines = []
    for i in range(n):
        start = i * 3000
        end = start + 2000
        lines += [
            str(i + 1),
            f"00:00:{start//1000:02d},000 --> 00:00:{end//1000:02d},000",
            "こんにちはみなさん",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    app = QApplication(sys.argv)
    s = AppSettings()
    s.parallel_files = 2

    tmp = Path(tempfile.mkdtemp())
    f1, f2 = tmp / "a.srt", tmp / "b.srt"
    make_srt(f1, 5)
    make_srt(f2, 5)

    # 替换 LLM 工厂
    T.TranslatorWorker._get_llm_service = lambda self: FakeLLM()

    facade = T.TranslatorFacade()
    facade.prepare_translation([str(f1), str(f2)])

    started, approved, completed, results = [], [], [], {}

    def on_waiting(fp):
        # 模拟用户确认：用面板当前文件路由
        approved.append(fp)
        facade.approve_analysis(fp, None)

    facade.signals.waiting_for_approval.connect(on_waiting)
    facade.signals.file_started.connect(lambda fp: started.append(fp))
    facade.signals.analysis_approved.connect(lambda fp: None)
    facade.signals.all_completed.connect(lambda r: (completed.append(True), results.update(r)))

    facade.start()

    # 事件循环直到 all_completed 或超时
    deadline = time.time() + 30
    while not completed and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)

    app.processEvents()

    print(f"并行文件数 = {s.parallel_files}")
    print(f"file_started: {len(started)} 个文件 (期望 2): {[Path(p).name for p in started]}")
    print(f"waiting_for_approval: {len(approved)} 个 (期望 2): {[Path(p).name for p in approved]}")
    print(f"all_completed 触发: {bool(completed)}, 结果: {len(results)} 个文件")

    out_a = tmp / "a_zh.srt"
    out_b = tmp / "b_zh.srt"
    ok_a = out_a.exists() and "你好啊" in out_a.read_text(encoding="utf-8")
    ok_b = out_b.exists() and "你好啊" in out_b.read_text(encoding="utf-8")

    assert len(started) == 2, "应处理 2 个文件"
    assert len(approved) == 2, "两个文件都应走确认流程"
    assert completed and len(results) == 2, "应合并 2 个文件结果"
    assert ok_a and ok_b, "两个输出文件都应生成且包含译文"
    print("两个文件并行翻译并各自确认 ✓ 输出文件: a_zh.srt, b_zh.srt")

    facade._force_cleanup()
    print("\nP1 并行翻译端到端测试通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
