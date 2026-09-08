"""RateLimiter 单元测试 — token 桶行为、超容量钳制、返还、进程级单例"""

import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.rate_limiter import RateLimiter, get_rate_limiter


class _FakeTime:
    """确定性时钟：sleep 只推进虚拟时间，不真正等待。

    限流器的桶残余量取决于等待期间按时间补充的配额，
    用真实时钟断言会被机器调度抖动干扰（本地毫秒级、CI 上可能差几十毫秒），
    注入本时钟后等待时长与残余量都完全可预测。
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_time(monkeypatch):
    """把 services.rate_limiter 模块内的 time 替换为确定性时钟。

    注意：RateLimiter 必须在 fixture 生效后构造，这样 last_refill 才取自虚拟时间。
    """
    import services.rate_limiter as rl_mod

    ft = _FakeTime()
    monkeypatch.setattr(rl_mod, "time", ft)
    return ft


class TestInit:
    def test_initial_half_bucket(self):
        rl = RateLimiter(tokens_per_minute=100, requests_per_minute=10)
        assert rl.token_bucket == 50
        assert rl.request_bucket == 5

    def test_buckets_capped_at_capacity(self):
        rl = RateLimiter(tokens_per_minute=100, requests_per_minute=10)
        rl.token_bucket = 999
        rl.request_bucket = 999
        rl._refill()
        assert rl.token_bucket == 100
        assert rl.request_bucket == 10


class TestAcquire:
    def test_sufficient_quota_immediate(self):
        rl = RateLimiter(tokens_per_minute=1000, requests_per_minute=100)
        start = time.monotonic()
        rl.acquire(estimated_tokens=100)
        assert time.monotonic() - start < 0.5
        assert rl.token_bucket == pytest.approx(400, abs=5)   # 500 - 100 + 少量补充
        assert rl.request_bucket == pytest.approx(49, abs=2)  # 50 - 1

    def test_zero_token_request_only_consumes_request_slot(self):
        rl = RateLimiter(tokens_per_minute=1000, requests_per_minute=100)
        before = rl.token_bucket
        rl.acquire(estimated_tokens=0)
        assert rl.token_bucket == pytest.approx(before, abs=5)
        assert rl.request_bucket == pytest.approx(49, abs=2)

    def test_token_shortage_waits_then_passes(self, fake_time):
        """桶内 token 不足 → 等待补充后放行（确定性时钟，无机器抖动）"""
        rl = RateLimiter(tokens_per_minute=60000, requests_per_minute=6000)
        rl.token_bucket = 0.0
        rl.acquire(estimated_tokens=100)  # 1000 tok/s 补充 → 需 0.1s
        assert fake_time.now == pytest.approx(0.1, abs=0.02)
        assert rl.token_bucket == pytest.approx(0, abs=0.01)

    def test_request_shortage_waits(self, fake_time):
        rl = RateLimiter(tokens_per_minute=60000, requests_per_minute=6000)
        rl.request_bucket = 0.0
        rl.acquire(estimated_tokens=0)  # 100 req/s 补充 → 需 0.01s
        assert fake_time.now == pytest.approx(0.01, abs=0.005)
        assert rl.request_bucket == pytest.approx(0, abs=0.01)

    def test_oversize_request_clamped_to_capacity(self):
        """超过桶容量的请求被钳制到容量，等桶满后整桶扣空，不无限挂死"""
        rl = RateLimiter(tokens_per_minute=1000, requests_per_minute=600)
        rl.token_bucket = 1000.0  # 预置满桶
        start = time.monotonic()
        rl.acquire(estimated_tokens=999_999)  # 远超容量
        assert time.monotonic() - start < 1.0
        assert rl.token_bucket == 0.0

    def test_concurrent_acquire_thread_safe(self, fake_time):
        """并发 acquire 不丢扣减、不崩溃（确定性时钟 → 无补充干扰，扣减必须精确）"""
        rl = RateLimiter(tokens_per_minute=600000, requests_per_minute=6000)
        rl.request_bucket = 50.0
        n = 40
        barrier = threading.Barrier(n)

        def worker():
            barrier.wait()
            rl.acquire(estimated_tokens=0)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert rl.request_bucket == pytest.approx(10.0, abs=0.01)  # 50 - 40


class TestRefund:
    def test_refund_tokens_and_requests(self):
        rl = RateLimiter(tokens_per_minute=1000, requests_per_minute=100)
        rl.token_bucket = 0.0
        rl.request_bucket = 0.0
        rl.refund(tokens=200, requests=2)
        assert rl.token_bucket == pytest.approx(200, abs=0.01)
        assert rl.request_bucket == pytest.approx(2, abs=0.01)

    def test_refund_capped_at_capacity(self):
        rl = RateLimiter(tokens_per_minute=1000, requests_per_minute=100)
        rl.token_bucket = 900.0
        rl.refund(tokens=500, requests=0)
        assert rl.token_bucket == 1000

    def test_refund_zero_is_noop(self):
        rl = RateLimiter(tokens_per_minute=1000, requests_per_minute=100)
        before = (rl.token_bucket, rl.request_bucket)
        rl.refund(tokens=0, requests=0)
        assert rl.token_bucket == pytest.approx(before[0], abs=0.01)
        assert rl.request_bucket == pytest.approx(before[1], abs=0.01)


class TestSharedLimiter:
    def test_same_key_same_instance(self):
        a = get_rate_limiter(12345, 67)
        b = get_rate_limiter(12345, 67)
        assert a is b

    def test_different_key_different_instance(self):
        a = get_rate_limiter(111, 11)
        b = get_rate_limiter(222, 22)
        assert a is not b
