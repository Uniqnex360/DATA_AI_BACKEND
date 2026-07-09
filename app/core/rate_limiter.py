# app/core/rate_limiter.py
import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, Optional
import logging
from collections import deque

logger = logging.getLogger("rate_limiter")

class OpenAIRateLimiter:
    """
    Token-based rate limiter for OpenAI API
    
    Limits: 
    - 30,000 tokens per minute (TPM)
    - 500 requests per minute (RPM)
    """
    
    def __init__(self, tpm_limit: int = 24000, rpm_limit: int = 400):
        self.tpm_limit = tpm_limit
        self.rpm_limit = rpm_limit
        
        # Token tracking
        self.tokens_used_this_minute = 0
        self.token_reset_time = time.time() + 60
        
        # Request tracking (sliding window)
        self.request_timestamps = deque()
        
        self._lock = asyncio.Lock()
    
    async def wait_if_needed(self, estimated_tokens: int = 1000) -> None:
        """
        Wait if rate limits would be exceeded
        
        Args:
            estimated_tokens: Estimated tokens for this request
        """
        async with self._lock:
            now = time.time()
            
            # ========================================
            # 1. Token-based rate limiting
            # ========================================
            # Reset token counter if minute has passed
            if now > self.token_reset_time:
                logger.info(f"Token window reset. Used {self.tokens_used_this_minute}/{self.tpm_limit} tokens")
                self.tokens_used_this_minute = 0
                self.token_reset_time = now + 60
            
            # Check if we'll exceed token limit
            if self.tokens_used_this_minute + estimated_tokens > self.tpm_limit:
                wait_time = self.token_reset_time - now
                await asyncio.sleep(wait_time)
                now=time.time()
                # logger.warning(f"Token limit approaching: {self.tokens_used_this_minute}/{self.tpm_limit}")
                # logger.warning(f"Waiting {wait_time:.2f}s for token reset")
                
                # Reset after waiting
                self.tokens_used_this_minute = 0
                self.token_reset_time = now + 60
            
            # ========================================
            # 2. Request-based rate limiting
            # ========================================
            # Remove requests older than 60 seconds
            while self.request_timestamps and self.request_timestamps[0] < now - 60:
                self.request_timestamps.popleft()
            
            # Check if we'll exceed request limit
            if len(self.request_timestamps) >= self.rpm_limit:
                oldest = self.request_timestamps[0]
                wait_time = 60 - (now - oldest)
                logger.warning(f"Request limit reached: {len(self.request_timestamps)}/{self.rpm_limit}")
                logger.warning(f"Waiting {wait_time:.2f}s for request slot")
                await asyncio.sleep(wait_time)
                now=time.time()
                
                # Remove expired requests after waiting
                while self.request_timestamps and self.request_timestamps[0] < now - 60:
                    self.request_timestamps.popleft()
            
            # Add current request timestamp
            self.request_timestamps.append(now)
            
            # Reserve tokens for this request
            self.tokens_used_this_minute += estimated_tokens
            
            logger.info(f"Rate limit status: {self.tokens_used_this_minute}/{self.tpm_limit} tokens, "
                        f"{len(self.request_timestamps)}/{self.rpm_limit} requests")

# Global rate limiter instance
openai_limiter = OpenAIRateLimiter()