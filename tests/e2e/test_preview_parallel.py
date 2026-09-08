"""并行预览状态隔离 + 单条重译/保存 测试"""
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
        return '{"topic_segments": [], "glossary": {}, "style_guide": ""}'

    def translate_batch(self, batch, src, tgt):
        n = batch.primary_end_idx - batch.primary_start_idx
        if n == 1:
            return ["重译单条"]  # 单条重译的返回值，便于断言
        return ["译文" + str(i) for i in range(n)]

    def validate_api_key(self):
        return True


def make_srt(path: Path, n: int, tag: str) -> None:
    lines = []
    for i in range(n):
        lines += [str(i + 1), f"00:00:0{i},000 --> 00:00:0{i+1},000", tag, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    app = QApplication(sys.argv)
    s = AppSettings()
    s.parallel_files = 2
    s.provider = "openai"
    s.set_api_key("openai", "fake-key-for-test")
    T.TranslatorWorker._get_llm_service = lambda self: FakeLLM()

    tmp = Path(tempfile.mkdtemp())
    f1, f2 = tmp / "p1.srt", tmp / "p2.srt"
    make_srt(f1, 3, "A")
    make_srt(f2, 3, "B")

    from gui.main_window import MainWindow
    # 主窗口的重译路径走它自己的 _get_llm_service，也要替换成 fake
    MainWindow._get_llm_service = lambda self: FakeLLM()
    w = MainWindow()
    w.show()
    w.file_panel.add_files([str(f1), str(f2)])
    w._on_translate()

    completed = []
    w.translator.signals.all_completed.connect(lambda r: completed.append(r))
    deadline = time.time() + 30
    while not completed and time.time() < deadline:
        app.processEvents()
        if w.analysis_panel.isVisible() and w.analysis_panel.approve_btn.isEnabled():
            w.analysis_panel._on_approve()
        time.sleep(0.02)
    app.processEvents()

    # 1) 预览状态按文件隔离
    keys = sorted(Path(k).name for k in w._current_results)
    print("预览状态按文件隔离 keys:", keys)
    assert keys == ["p1.srt", "p2.srt"], f"应按文件分组, 实际 {keys}"
    for k in w._current_results:
        idxs = sorted(w._current_results[k].keys())
        assert idxs == [0, 1, 2], f"{k} 的索引应为 [0,1,2], 实际 {idxs}"
    print("每个文件各自持有独立结果 ✓")

    # 2) 单条重译：模拟预览面板发出重译请求
    w._current_file = str(f1)
    w._on_retranslate_entry(1)
    deadline = time.time() + 15
    while not completed and time.time() < deadline:
        pass  # 等重译后台任务完成（信号在主循环）
    # 处理重译回调
    for _ in range(100):
        app.processEvents()
        time.sleep(0.02)
    r1 = w._current_results[str(f1)].get(1)
    print("重译后第 2 条状态:", r1.status if r1 else None)
    assert r1 and r1.status == "manually_edited", "重译后应标记为 manually_edited"

    # 3) 重译自动保存到 _zh 文件
    out = tmp / "p1_zh.srt"
    assert out.exists(), "_zh 文件应存在"
    content = out.read_text(encoding="utf-8")
    print("_zh 文件已更新:", "重译单条" in content)
    assert "重译单条" in content, "重译结果应已写回文件"

    # 4) 「保存修改」按钮应已启用
    assert w.save_edits_btn.isEnabled(), "保存修改按钮应启用"
    print("保存修改按钮已启用 ✓")

    if not completed:
        w.translator.cancel()
    w.translator._force_cleanup()
    w.close()
    print("\n并行预览隔离 + 重译保存测试通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
