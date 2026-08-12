import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, Optional
import logging
from collections import deque
logger = logging.getLogger("rate_limiter")
class OpenAIRateLimiter:
    def __init__(self, tpm_limit: int = 24000, rpm_limit: int = 400):
        self.tpm_limit = tpm_limit
        self.rpm_limit = rpm_limit
        self.tokens_used_this_minute = 0
        self.token_reset_time = time.time() + 60
        self.request_timestamps = deque()
        self._lock = asyncio.Lock()
    async def wait_if_needed(self, estimated_tokens: int = 1000) -> None:
        async with self._lock:
            now = time.time()
            if now > self.token_reset_time:
                logger.info(f"Token window reset. Used {self.tokens_used_this_minute}/{self.tpm_limit} tokens")
                self.tokens_used_this_minute = 0
                self.token_reset_time = now + 60
            if self.tokens_used_this_minute + estimated_tokens > self.tpm_limit:
                wait_time = self.token_reset_time - now
                await asyncio.sleep(wait_time)
                now=time.time()
                self.tokens_used_this_minute = 0
                self.token_reset_time = now + 60
            while self.request_timestamps and self.request_timestamps[0] < now - 60:
                self.request_timestamps.popleft()
            if len(self.request_timestamps) >= self.rpm_limit:
                oldest = self.request_timestamps[0]
                wait_time = 60 - (now - oldest)
                logger.warning(f"Request limit reached: {len(self.request_timestamps)}/{self.rpm_limit}")
                logger.warning(f"Waiting {wait_time:.2f}s for request slot")
                await asyncio.sleep(wait_time)
                now=time.time()
                while self.request_timestamps and self.request_timestamps[0] < now - 60:
                    self.request_timestamps.popleft()
            self.request_timestamps.append(now)
            self.tokens_used_this_minute += estimated_tokens
            logger.info(f"Rate limit status: {self.tokens_used_this_minute}/{self.tpm_limit} tokens, "
                        f"{len(self.request_timestamps)}/{self.rpm_limit} requests")
openai_limiter = OpenAIRateLimiter()