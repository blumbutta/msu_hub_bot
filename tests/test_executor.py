"""Bounded, offline regression tests for the shared application worker pool."""

import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures.thread import BrokenThreadPool

import pytest

from common.executor import TPExecutor


class CountingPool(ThreadPoolExecutor):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.submitted = 0

    def submit(self, function, *args, **kwargs):
        self.submitted += 1
        return super().submit(function, *args, **kwargs)


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("workers", [1, 3])
@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_abandoned_work_keeps_its_slot(workers, stop):
    executor = TPExecutor(workers)
    executor.ExecutorClass = CountingPool
    release = threading.Event()
    started = [threading.Event() for _ in range(workers)]

    def block(index):
        started[index].set()
        assert release.wait(3), "test worker was not released"

    jobs = [asyncio.create_task(executor.run(block, index, timeout=0.1 if stop == "timeout" else None)) for index in range(workers)]
    try:
        await wait_until(lambda: all(event.is_set() for event in started))
        if stop == "cancel":
            for job in jobs:
                job.cancel()
            results = await asyncio.gather(*jobs, return_exceptions=True)
            assert all(isinstance(result, asyncio.CancelledError) for result in results)
        else:
            assert await asyncio.gather(*jobs) == [(None, True)] * workers

        assert await asyncio.wait_for(executor.run(lambda: "late", timeout=0.02), 0.5) == (None, True)
        # A timed-out caller must not admit another job to the pool's unbounded queue.
        assert executor.executor.submitted == workers
        release.set()
        assert await executor.run(lambda: "recovered", timeout=1) == ("recovered", False)
    finally:
        release.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        executor.shutdown(wait=True)


async def test_timeout_includes_waiting_for_a_worker():
    executor = TPExecutor(1)
    release = threading.Event()
    started = threading.Event()

    def block():
        started.set()
        assert release.wait(3), "test worker was not released"

    first = asyncio.create_task(executor.run(block, timeout=None))
    try:
        await wait_until(started.is_set)
        assert await asyncio.wait_for(executor.run(lambda: "queued", timeout=0.02), 0.5) == (None, True)
    finally:
        release.set()
        await first
        executor.shutdown(wait=True)


class BrokenPool(ThreadPoolExecutor):
    def submit(self, *args, **kwargs):
        raise BrokenThreadPool("synthetic pool failure")


@pytest.mark.parametrize("workers", [1, 3])
async def test_broken_pool_retries_without_deadlock_or_extra_arguments(workers):
    executor = TPExecutor(workers)
    pools = []

    def factory(**kwargs):
        pool = (BrokenPool if not pools else CountingPool)(**kwargs)
        pools.append(pool)
        return pool

    executor.ExecutorClass = factory
    try:
        assert await asyncio.wait_for(executor.run(lambda value: value, "ok", timeout=1), 0.5) == ("ok", False)
        assert len(pools) == 2
    finally:
        executor.shutdown(wait=True)


async def test_broken_pool_recovery_has_a_retry_limit():
    executor = TPExecutor(1)
    pools = []

    def factory(**kwargs):
        pool = BrokenPool(**kwargs)
        pools.append(pool)
        return pool

    executor.ExecutorClass = factory
    try:
        with pytest.raises(BrokenThreadPool, match="synthetic"):
            await asyncio.wait_for(executor.run(lambda: None, timeout=1), 0.5)
        assert len(pools) == 2
    finally:
        executor.shutdown(wait=True)


async def test_worker_timeout_error_is_not_a_caller_timeout():
    executor = TPExecutor(1)

    def fail():
        raise TimeoutError("provider deadline")

    try:
        with pytest.raises(TimeoutError, match="provider deadline"):
            await executor.run(fail)
        assert await executor.run(lambda: "ok") == ("ok", False)
    finally:
        executor.shutdown(wait=True)


async def test_shutdown_does_not_create_a_pool_and_rejects_new_work():
    executor = TPExecutor(1)
    executor.shutdown(wait=False)
    assert executor._executor is None
    with pytest.raises(RuntimeError, match="shutdown"):
        await executor.run(lambda: None)
    executor.shutdown(wait=False)


@pytest.mark.parametrize("timeout", [0, -1])
async def test_expired_deadline_does_not_start_work(timeout):
    executor = TPExecutor(1)
    started = threading.Event()
    try:
        assert await executor.run(started.set, timeout=timeout) == (None, True)
        assert not started.is_set()
        assert executor._executor is None
    finally:
        executor.shutdown(wait=True)


@pytest.mark.parametrize("workers", [0, -1, 0.5, True])
def test_invalid_worker_limit_rejected(workers):
    with pytest.raises(ValueError):
        TPExecutor(workers)


async def test_cancelled_queue_waiter_never_runs_or_loses_capacity():
    executor = TPExecutor(1)
    release = threading.Event()
    started = threading.Event()
    queued_started = threading.Event()

    def block():
        started.set()
        assert release.wait(3), "test worker was not released"

    first = asyncio.create_task(executor.run(block))
    queued = None
    try:
        await wait_until(started.is_set)
        queued = asyncio.create_task(executor.run(queued_started.set))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release.set()
        await first
        assert await executor.run(lambda: "ok", timeout=1) == ("ok", False)
        assert not queued_started.is_set()
    finally:
        release.set()
        await asyncio.gather(first, *([queued] if queued is not None else []), return_exceptions=True)
        executor.shutdown(wait=True)


async def test_concurrent_broken_futures_share_one_replacement_pool():
    executor = TPExecutor(3)
    futures = []
    pools = []

    class BrokenFuturePool(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            future = Future()
            futures.append(future)
            return future

    def factory(**kwargs):
        pool = (BrokenFuturePool if not pools else CountingPool)(**kwargs)
        pools.append(pool)
        return pool

    executor.ExecutorClass = factory
    jobs = [asyncio.create_task(executor.run(lambda value: value, index, timeout=1)) for index in range(3)]
    try:
        await wait_until(lambda: len(futures) == 3)
        for future in futures:
            future.set_exception(BrokenThreadPool("synthetic future failure"))
        assert await asyncio.wait_for(asyncio.gather(*jobs), 1) == [(index, False) for index in range(3)]
        assert len(pools) == 2
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        executor.shutdown(wait=True)


async def test_application_applet_uses_three_threads_and_closes_them():
    from common.applets import AppCPUExecutor

    applet = AppCPUExecutor()
    applet.init()
    try:
        assert isinstance(applet.cpu_executor, TPExecutor)
        assert applet.cpu_executor.max_workers == 3
        worker, timeouted = await applet.cpu_executor.run(threading.get_ident)
        assert worker != threading.get_ident() and not timeouted
    finally:
        await applet.on_shutdown()
    with pytest.raises(RuntimeError, match="shutdown"):
        await applet.cpu_executor.run(lambda: None)
