"""Offline regression tests: no database, documents, or paid APIs."""

import threading
import time
import unittest

from creditpaper.ledger.worker import run_all_tenants


class WorkerSchedulingTests(unittest.TestCase):
    def test_empty_queue_is_backed_off(self):
        stop = threading.Event()
        times = []

        def job(_tenant):
            times.append(time.monotonic())
            if len(times) == 2:
                stop.set()
            return False

        run_all_tenants(
            concurrency=1, per_tenant=1, discover=lambda: ["a"], run_one=job, stop=stop, idle_sleep_s=0.05
        )
        self.assertEqual(len(times), 2)
        self.assertGreaterEqual(times[1] - times[0], 0.05)

    def test_once_stays_bounded(self):
        seen = []
        run_all_tenants(
            concurrency=2,
            per_tenant=2,
            discover=lambda: ["a", "b"],
            run_one=lambda tenant: seen.append(tenant) or True,
            once=True,
        )
        self.assertCountEqual(seen, ["a", "a", "b", "b"])

    def test_stopped_worker_does_not_claim(self):
        stop = threading.Event()
        stop.set()
        seen = []
        run_all_tenants(discover=lambda: ["a"], run_one=lambda t: seen.append(t), stop=stop)
        self.assertEqual(seen, [])

    def test_free_slot_is_refilled_before_slow_job_finishes(self):
        stop = threading.Event()
        slow_started = threading.Event()
        refilled = threading.Event()
        lock = threading.Lock()
        calls = 0

        def job(_tenant):
            nonlocal calls
            with lock:
                calls += 1
                number = calls
            if number == 1:
                slow_started.set()
                refilled.wait(2)
                stop.set()
            elif number == 2:
                slow_started.wait(1)
            else:
                refilled.set()
                stop.set()
            return True

        run_all_tenants(
            concurrency=2,
            per_tenant=2,
            discover=lambda: ["a"],
            run_one=job,
            stop=stop,
            idle_sleep_s=0.01,
        )
        self.assertTrue(refilled.is_set(), "Free worker waited behind the slow job")

    def test_tenant_limit_and_fairness(self):
        stop = threading.Event()
        barrier = threading.Barrier(2, timeout=2)
        seen = []
        lock = threading.Lock()

        def job(tenant):
            with lock:
                seen.append(tenant)
            barrier.wait()
            stop.set()
            return True

        run_all_tenants(
            concurrency=2,
            per_tenant=1,
            discover=lambda: ["a", "b"],
            run_one=job,
            stop=stop,
            idle_sleep_s=0.01,
        )
        self.assertCountEqual(seen, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
