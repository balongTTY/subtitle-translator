"""Token 桶速率限制器"""

import time
import threading
import logging

log = logging.getLogger("subtitle_translator")


class RateLimiter:
    """Token 桶速率控制"""

    def __init__(self, tokens_per_minute: int = 90000, requests_per_minute: int = 60):
        self.tokens_per_minute = tokens_per_minute
        self.requests_per_minute = requests_per_minute
        # 初始只给半桶：满桶启动允许瞬间突发整分钟配额，与 provider 的滚动
        # 窗口语义不符，冷启动易触发 429
        self.token_bucket: float = tokens_per_minute / 2
        self.request_bucket: float = requests_per_minute / 2
        self.last_refill: float = time.monotonic()
        self.lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.token_bucket = min(
            self.tokens_per_minute,
            self.token_bucket + elapsed * (self.tokens_per_minute / 60),
        )
        self.request_bucket = min(
            self.requests_per_minute,
            self.request_bucket + elapsed * (self.requests_per_minute / 60),
        )
        self.last_refill = now

    def acquire(self, estimated_tokens: int = 0) -> None:
        # 不在持锁期间 sleep：大请求等待配额时释放锁，
        # 让配额充足的小请求可以插队（避免队头阻塞拖垮并行 worker）
        while True:
            with self.lock:
                self._refill()
                # 超过桶容量的请求永远等不满（桶被钳制在容量内）——
                # 钳制到容量并等到桶满即放行、整桶扣空，等待上界为一个
                # 填充周期（≤60s），绝不能无限挂死 worker 线程
                need = min(estimated_tokens, self.tokens_per_minute) if estimated_tokens > 0 else 0
                wait = 0.0
                if self.request_bucket < 1:
                    wait = max(
                        wait,
                        (1 - self.request_bucket) * (60 / self.requests_per_minute),
                    )
                if need > 0 and self.token_bucket < need:
                    wait = max(
                        wait,
                        (need - self.token_bucket)
                        * (60 / self.tokens_per_minute),
                    )
                if wait <= 0:
                    self.request_bucket -= 1
                    if need > 0:
                        self.token_bucket = max(0.0, self.token_bucket - need)
                    return
            # 锁外等待，最大 0.5s 一次，让其他请求有机会先获得配额
            time.sleep(min(wait, 0.5))

    def refund(self, tokens: float = 0, requests: float = 1) -> None:
        """请求失败时返还预扣配额。

        acquire 在请求前预扣 token/请求数，失败重试会再次 acquire——
        不返还的话同一批次扣 2~3 倍配额，限流器比真实用量提前进入等待。
        限流器是记账性的：失败请求大概率没消耗配额，宁可返还宽裕。
        """
        with self.lock:
            self._refill()
            if tokens > 0:
                self.token_bucket = min(
                    self.tokens_per_minute, self.token_bucket + tokens
                )
            if requests > 0:
                self.request_bucket = min(
                    self.requests_per_minute, self.request_bucket + requests
                )


# ---------------------------------------------------------------------------
#  全局共享限流器：同一 provider 的所有 worker 共用一个实例，
#  并行翻译时不会各自超发配额
# ---------------------------------------------------------------------------

_shared_limiters: dict[tuple[int, int], RateLimiter] = {}
_limiters_lock = threading.Lock()


def get_rate_limiter(tokens_per_minute: int, requests_per_minute: int) -> RateLimiter:
    """按 (tpm, rpm) 返回进程级单例限流器"""
    key = (tokens_per_minute, requests_per_minute)
    with _limiters_lock:
        if key not in _shared_limiters:
            _shared_limiters[key] = RateLimiter(tokens_per_minute, requests_per_minute)
        return _shared_limiters[key]
