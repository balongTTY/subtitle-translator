"""一键运行全部端到端验证脚本

用法: python tests/e2e/run_e2e.py
每个脚本在独立子进程中运行（它们都会创建 QApplication 单例），
避免相互干扰。返回非 0 表示有失败。

单元测试（pytest）请直接运行: pytest
"""

import subprocess
import sys
import time
from pathlib import Path

# Windows 控制台可能是 GBK，强制 UTF-8 输出避免编码异常
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

E2E_DIR = Path(__file__).resolve().parent
ROOT = E2E_DIR.parent.parent
TESTS = [
    "test_parallel.py",
    "test_parallel_gui.py",
    "test_preview_parallel.py",
    "test_real_files.py",
    "test_parser.py",
    "test_r2_verify.py",
    "test_extractor.py",
    "test_review_fixes.py",
    "test_review_round2.py",
    "test_asr_online.py",
    "test_model_download.py",
]


def main() -> int:
    results = []
    for name in TESTS:
        path = E2E_DIR / name
        print(f"\n=== 运行 {name} ===", flush=True)
        t0 = time.time()
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
        elapsed = time.time() - t0
        ok = proc.returncode == 0
        results.append((name, ok, elapsed))
        # 打印测试输出尾部（成功时最后几行，失败时全部）
        lines = proc.stdout.strip().splitlines()
        tail = lines[-4:] if ok else (lines + proc.stderr.strip().splitlines())[-20:]
        for line in tail:
            print(f"  {line}")
        if not ok:
            print(f"  [失败] 退出码 {proc.returncode}")

    print("\n" + "=" * 50)
    print("测试结果汇总:")
    all_ok = True
    for name, ok, elapsed in results:
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"  {status}  {name}  ({elapsed:.1f}s)")
        all_ok = all_ok and ok
    print(f"\n总结: {'全部通过' if all_ok else '存在失败'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
