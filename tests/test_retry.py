"""Tests for the retry decorator."""

import asyncio
import pytest

from callstack.errors import ATTimeoutError, ATCommandError, TransportError
from callstack.utils.retry import retry


class TestRetry:
    async def test_succeeds_first_try(self):
        call_count = 0

        @retry(max_attempts=3, base_delay=0.01)
        async def succeed():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await succeed()
        assert result == "ok"
        assert call_count == 1

    async def test_retries_on_timeout_error(self):
        call_count = 0

        @retry(max_attempts=3, base_delay=0.01)
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ATTimeoutError("timeout")
            return "ok"

        result = await flaky()
        assert result == "ok"
        assert call_count == 3

    async def test_retries_on_transport_error(self):
        call_count = 0

        @retry(max_attempts=2, base_delay=0.01)
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise TransportError("disconnected")
            return "ok"

        result = await flaky()
        assert result == "ok"
        assert call_count == 2

    async def test_raises_after_max_attempts(self):
        call_count = 0

        @retry(max_attempts=3, base_delay=0.01)
        async def always_fail():
            nonlocal call_count
            call_count += 1
            raise ATTimeoutError("timeout")

        with pytest.raises(ATTimeoutError):
            await always_fail()
        assert call_count == 3

    async def test_does_not_retry_non_retryable(self):
        call_count = 0

        @retry(max_attempts=3, base_delay=0.01)
        async def raise_value_error():
            nonlocal call_count
            call_count += 1
            raise ValueError("not retryable")

        with pytest.raises(ValueError):
            await raise_value_error()
        assert call_count == 1

    async def test_custom_retryable_types(self):
        call_count = 0

        @retry(max_attempts=2, base_delay=0.01, retryable=(ValueError,))
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError("retry me")
            return "ok"

        result = await flaky()
        assert result == "ok"
        assert call_count == 2

    async def test_backoff_increases_delay(self):
        """Verify exponential backoff by checking elapsed time."""
        call_count = 0

        @retry(max_attempts=3, base_delay=0.05, backoff_factor=2.0)
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ATTimeoutError("timeout")
            return "ok"

        start = asyncio.get_event_loop().time()
        await flaky()
        elapsed = asyncio.get_event_loop().time() - start

        # base_delay=0.05, then 0.1 => total ~0.15s minimum
        assert elapsed >= 0.12
        assert call_count == 3

    async def test_max_delay_caps_backoff(self):
        call_count = 0

        @retry(max_attempts=4, base_delay=0.05, backoff_factor=100.0, max_delay=0.06)
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 4:
                raise ATTimeoutError("timeout")
            return "ok"

        start = asyncio.get_event_loop().time()
        await flaky()
        elapsed = asyncio.get_event_loop().time() - start

        # 3 retries, each capped at 0.06s => total ~0.18s max
        assert elapsed < 0.5
        assert call_count == 4

    async def test_preserves_function_name(self):
        @retry(max_attempts=2)
        async def my_function():
            pass

        assert my_function.__name__ == "my_function"
