import asyncio
from concurrent.futures.process import ProcessPoolExecutor, BrokenProcessPool
from concurrent.futures.thread import ThreadPoolExecutor, BrokenThreadPool
from contextlib import suppress
from typing import Any, Callable, Tuple, Optional

from common.utils import do_nothing


class _BaseExecutor:
    ExecutorClass = None
    ExecutorException = None

    def __init__(self, max_workers: int):
        if not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers <= 0:
            raise ValueError('max_workers must be a positive integer')
        self.max_workers = max_workers
        self._slots = asyncio.BoundedSemaphore(max_workers)
        self._closed = False

        # Lazy initialization
        self._executor = None

    @property
    def executor(self):
        if self._closed:
            raise RuntimeError('cannot schedule new futures after shutdown')
        if self._executor:
            return self._executor
        self._executor = self.ExecutorClass(max_workers=self.max_workers)
        return self._executor

    async def run(self, func: Callable, *args, timeout: Optional[float] = 180) -> Tuple[Any, bool]:
        """Apply a caller deadline to queueing, execution, and one pool recovery.

        Timeout/cancellation cannot stop a running thread. Its slot stays occupied
        until the concurrent future finishes; jobs need their own I/O/process limits.
        Use this executor from a single application event loop.
        """
        if self._closed:
            raise RuntimeError('cannot schedule new futures after shutdown')
        if timeout is not None and timeout <= 0:
            return None, True
        loop = asyncio.get_running_loop()
        limit = asyncio.timeout(timeout)

        def release_slot(_future):
            # Runs in a worker thread, possibly after the application loop has closed.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._slots.release)

        try:
            async with limit:
                for attempt in range(2):
                    pool = None
                    try:
                        await self._slots.acquire()
                        try:
                            pool = self.executor
                            future = pool.submit(func, *args)
                        except BaseException:
                            self._slots.release()
                            raise
                        # Track the real work, not the cancellable asyncio wrapper.
                        future.add_done_callback(release_slot)
                        return await asyncio.wrap_future(future), False
                    except self.ExecutorException:
                        if pool is not None and pool is self._executor:
                            self._executor = None
                            pool.shutdown(wait=False, cancel_futures=True)
                        if attempt:
                            raise
        except TimeoutError:
            if limit.expired():
                return None, True
            # A TimeoutError raised by the worker is a genuine job failure.
            raise

    async def run_here(self, func: Callable, *args, timeout: float = None) -> Tuple[Any, bool]:
        """
        Useful analog for debugging
        """
        do_nothing(self, timeout)
        result = func(*args)
        return result, False

    def shutdown(self, wait: bool):
        """Reject new jobs and cancel queued futures; running threads are not killed."""
        self._closed = True
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=True)


class TPExecutor(_BaseExecutor):
    ExecutorClass = ThreadPoolExecutor
    ExecutorException = BrokenThreadPool


class PPExecutor(_BaseExecutor):
    ExecutorClass = ProcessPoolExecutor
    ExecutorException = BrokenProcessPool


PPExecutor = TPExecutor  # noqa: F811 — preserve the deployed thread executor


class FakePPExecutor(PPExecutor):
    def __init__(self, *args, **kwargs):
        pass

    async def run(self, func: Callable, *args, timeout: float = None) -> Tuple[Any, bool]:
        """
        Useful analog for debugging
        """
        do_nothing(self, timeout)
        result = func(*args)
        return result, False
