"""P1 GUI 集成测试：通过主窗口真实触发翻译，验证并行 + 面板确认路由"""
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
        return ["测试译文" for _ in range(batch.primary_end_idx - batch.primary_start_idx)]

    def validate_api_key(self):
        return True


def main() -> int:
    app = QApplication(sys.argv)
    s = AppSettings()
    s.parallel_files = 2
    s.provider = "openai"  # 显式指定，避免读用户真实 provider
    s.set_api_key("openai", "fake-key-for-test")
    T.TranslatorWorker._get_llm_service = lambda self: FakeLLM()

    tmp = Path(tempfile.mkdtemp())
    files = []
    for name in ("x.srt", "y.srt"):
        p = tmp / name
        lines = []
        for i in range(4):
            lines += [str(i + 1), f"00:00:0{i},000 --> 00:00:0{i+1},000", "こんにちは", ""]
        p.write_text("\n".join(lines), encoding="utf-8")
        files.append(str(p))

    from gui.main_window import MainWindow
    w = MainWindow()
    w.show()
    w.file_panel.add_files(files)
    assert w.translate_btn.isEnabled(), "翻译按钮应可用"
    w._on_translate()

    # 直接挂钩真实 facade 的聚合信号（_on_translate 内部已连接 UI 槽，这里额外记录）
    completed = []
    w.translator.signals.all_completed.connect(lambda r: completed.append(r))

    approved_files = []
    deadline = time.time() + 30
    while not completed and time.time() < deadline:
        app.processEvents()
        # 模拟用户点击「确认分析」
        if w.analysis_panel.isVisible() and w.analysis_panel.approve_btn.isEnabled():
            fp = w.analysis_panel.current_filepath
            approved_files.append(Path(fp).name)
            w.analysis_panel._on_approve()
        time.sleep(0.02)
    app.processEvents()

    if not completed:
        w.translator.cancel()  # 超时兜底：先停线程再退出，避免 teardown 崩溃
        app.processEvents()

    print(f"通过 UI 确认的文件: {approved_files}")
    print(f"完成结果文件数: {len(completed[0]) if completed else 0}")
    assert len(approved_files) == 2, f"两个文件都应在 UI 中确认, 实际 {approved_files}"
    assert completed, "all_completed 应触发"
    assert len(completed[0]) == 2, "应合并 2 个文件结果"

    w.translator._force_cleanup()
    w.close()
    print("P1 GUI 集成测试通过 ✓（并行 + 面板按文件路由确认）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
