"""TranslatorWorker._resolve_output_path 单元测试 — 输出备份链

不启动 QThread，直接调用方法；信号无连接时发射是安全的。
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.translator import TranslatorWorker


@pytest.fixture
def worker() -> TranslatorWorker:
    return TranslatorWorker(["dummy.srt"])


class TestResolveOutputPath:
    def test_fresh_path_no_backup(self, worker, tmp_path):
        src = tmp_path / "video.srt"
        src.write_text("1\n00:00:01,000 --> 00:00:02,000\nあ\n\n", encoding="utf-8")
        out = worker._resolve_output_path(str(src))
        assert Path(out).name == "video_zh.srt"
        assert Path(out).parent == tmp_path
        assert not Path(out).exists()

    def test_existing_output_backed_up(self, worker, tmp_path):
        src = tmp_path / "video.srt"
        src.write_text("x", encoding="utf-8")
        old_out = tmp_path / "video_zh.srt"
        old_out.write_text("旧的人工译文", encoding="utf-8")
        out = worker._resolve_output_path(str(src))
        assert out == str(old_out)
        backup = tmp_path / "video_zh.bak.srt"
        assert backup.exists()
        assert backup.read_text(encoding="utf-8") == "旧的人工译文"

    def test_backup_chain_bak1_bak2(self, worker, tmp_path):
        src = tmp_path / "video.srt"
        src.write_text("x", encoding="utf-8")
        (tmp_path / "video_zh.srt").write_text("v1", encoding="utf-8")
        (tmp_path / "video_zh.bak.srt").write_text("v0", encoding="utf-8")
        worker._resolve_output_path(str(src))
        assert (tmp_path / "video_zh.bak.srt").read_text(encoding="utf-8") == "v0"
        assert (tmp_path / "video_zh.bak1.srt").read_text(encoding="utf-8") == "v1"

    def test_long_backup_chain(self, worker, tmp_path):
        src = tmp_path / "video.srt"
        src.write_text("x", encoding="utf-8")
        (tmp_path / "video_zh.srt").write_text("v3", encoding="utf-8")
        (tmp_path / "video_zh.bak.srt").write_text("v2", encoding="utf-8")
        (tmp_path / "video_zh.bak1.srt").write_text("v1", encoding="utf-8")
        (tmp_path / "video_zh.bak2.srt").write_text("v0", encoding="utf-8")
        worker._resolve_output_path(str(src))
        assert (tmp_path / "video_zh.bak3.srt").read_text(encoding="utf-8") == "v3"

    def test_backup_failure_aborts_save(self, worker, tmp_path, monkeypatch):
        """备份失败 → 抛 OSError 中止本次保存（不再告警后直接覆盖唯一好翻译）"""
        src = tmp_path / "video.srt"
        src.write_text("x", encoding="utf-8")
        old_out = tmp_path / "video_zh.srt"
        old_out.write_text("旧", encoding="utf-8")

        def boom(self, target):
            raise OSError("模拟权限拒绝")

        monkeypatch.setattr(Path, "replace", boom)
        with pytest.raises(OSError, match="备份失败"):
            worker._resolve_output_path(str(src))
        assert old_out.exists()  # 旧文件未被移动、未被覆盖

    def test_non_srt_extension_preserved(self, worker, tmp_path):
        src = tmp_path / "video.ass"
        src.write_text("x", encoding="utf-8")
        out = worker._resolve_output_path(str(src))
        assert Path(out).name == "video_zh.ass"
