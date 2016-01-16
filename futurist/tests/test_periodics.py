# -*- coding: utf-8 -*-

# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import contextlib
import functools
import threading
import time

import eventlet
from eventlet.green import threading as green_threading
import testscenarios

import futurist
from futurist import periodics
from futurist.tests import base


@periodics.periodic(1)
def every_one_sec(cb):
    cb()


@periodics.periodic(0.5)
def every_half_sec(cb):
    cb()


@contextlib.contextmanager
def create_destroy_thread(run_what, *args, **kwargs):
    t = threading.Thread(target=run_what, args=args, kwargs=kwargs)
    t.daemon = True
    t.start()
    try:
        yield
    finally:
        t.join()


@contextlib.contextmanager
def create_destroy_green_thread(run_what, *args, **kwargs):
    t = eventlet.spawn(run_what, *args, **kwargs)
    try:
        yield
    finally:
        t.wait()


class TestPeriodicsStrategies(base.TestCase):
    def test_invalids(self):
        self.assertRaises(ValueError,
                          periodics.PeriodicWorker, [],
                          schedule_strategy='not_a_strategy')


class TestPeriodics(testscenarios.TestWithScenarios, base.TestCase):
    scenarios = [
        ('sync', {'executor_cls': futurist.SynchronousExecutor,
                  'executor_kwargs': {},
                  'create_destroy': create_destroy_thread,
                  'sleep': time.sleep,
                  'event_cls': threading.Event,
                  'worker_kwargs': {}}),
        ('thread', {'executor_cls': futurist.ThreadPoolExecutor,
                    'executor_kwargs': {'max_workers': 2},
                    'create_destroy': create_destroy_thread,
                    'sleep': time.sleep,
                    'event_cls': threading.Event,
                    'worker_kwargs': {}}),
        ('green', {'executor_cls': futurist.GreenThreadPoolExecutor,
                   'executor_kwargs': {'max_workers': 10},
                   'sleep': eventlet.sleep,
                   'event_cls': green_threading.Event,
                   'create_destroy': create_destroy_green_thread,
                   'worker_kwargs': {'cond_cls': green_threading.Condition,
                                     'event_cls': green_threading.Event}}),
    ]

    def _test_strategy(self, schedule_strategy, nows,
                       last_now, expected_next):
        nows = list(nows)
        ev = self.event_cls()

        def now_func():
            if len(nows) == 1:
                ev.set()
                return last_now
            return nows.pop()

        @periodics.periodic(2, run_immediately=False)
        def slow_periodic():
            pass

        callables = [
            (slow_periodic, None, None),
        ]
        worker_kwargs = self.worker_kwargs.copy()
        worker_kwargs['schedule_strategy'] = schedule_strategy
        worker_kwargs['now_func'] = now_func
        w = periodics.PeriodicWorker(callables, **worker_kwargs)

        with self.create_destroy(w.start):
            ev.wait()
            w.stop()

        schedule_order = w._schedule._ordering
        self.assertEqual([(expected_next, 0)], schedule_order)

    def test_last_finished_strategy(self):
        last_now = 3.2
        nows = [
            # Initial schedule building.
            0,
            # Worker run loop fetch time (to see how long to wait).
            2,
            # Function call start time.
            2,
            # Function call end time.
            3,
            # Stop.
            -1,
        ]
        nows = list(reversed(nows))
        self._test_strategy('last_finished', nows, last_now, 5.0)

    def test_waiting_immediate_add_processed(self):
        ran_at = []

        @periodics.periodic(0.1, run_immediately=True)
        def activated_periodic():
            ran_at.append(time.time())

        w = periodics.PeriodicWorker([], **self.worker_kwargs)
        with self.create_destroy(w.start, allow_empty=True):
            # Give some time for the thread to start...
            self.sleep(0.5)
            w.add(activated_periodic)
            while len(ran_at) == 0:
                self.sleep(0.1)
            w.stop()

    def test_double_start_fail(self):
        w = periodics.PeriodicWorker([], **self.worker_kwargs)
        with self.create_destroy(w.start, allow_empty=True):
            # Give some time for the thread to start...
            self.sleep(0.5)
            # Now ensure we can't start it again...
            self.assertRaises(RuntimeError, w.start)
            w.stop()

    def test_last_started_strategy(self):
        last_now = 3.2
        nows = [
            # Initial schedule building.
            0,
            # Worker run loop fetch time (to see how long to wait).
            2,
            # Function call start time.
            2,
            # Function call end time.
            3,
            # Stop.
            -1,
        ]
        nows = list(reversed(nows))
        self._test_strategy('last_started', nows, last_now, 4.0)

    def test_aligned_strategy(self):
        last_now = 5.5
        nows = [
            # Initial schedule building.
            0,
            # Worker run loop fetch time (to see how long to wait).
            2,
            # Function call start time.
            2,
            # Function call end time.
            5,
            # Stop.
            -1,
        ]
        nows = list(reversed(nows))
        self._test_strategy('aligned_last_finished', nows, last_now, 6.0)

    def test_add_on_demand(self):
        called = set()

        def cb(name):
            called.add(name)

        callables = []
        for i in range(0, 10):
            i_cb = functools.partial(cb, '%s_has_called' % i)
            callables.append((every_half_sec, (i_cb,), {}))

        leftover_callables = list(callables)
        w = periodics.PeriodicWorker([], **self.worker_kwargs)
        with self.create_destroy(w.start, allow_empty=True):
            # NOTE(harlowja): if this never happens, the test will fail
            # eventually, with a timeout error..., probably can make it fail
            # slightly faster in the future...
            while len(called) != len(callables):
                if leftover_callables:
                    cb, args, kwargs = leftover_callables.pop()
                    w.add(cb, *args, **kwargs)
                self.sleep(0.1)
            w.stop()

    def test_disabled(self):

        @periodics.periodic(0.5, enabled=False)
        def no_add_me():
            pass

        @periodics.periodic(0.5)
        def add_me():
            pass

        w = periodics.PeriodicWorker([], **self.worker_kwargs)
        self.assertEqual(0, len(w))
        self.assertIsNone(w.add(no_add_me))
        self.assertEqual(0, len(w))

        self.assertIsNotNone(w.add(add_me))
        self.assertEqual(1, len(w))

    def test_watcher(self):

        def cb():
            pass

        callables = [
            (every_one_sec, (cb,), None),
            (every_half_sec, (cb,), None),
        ]
        executor_factory = lambda: self.executor_cls(**self.executor_kwargs)
        w = periodics.PeriodicWorker(callables,
                                     executor_factory=executor_factory,
                                     **self.worker_kwargs)
        with self.create_destroy(w.start):
            self.sleep(2.0)
            w.stop()

        for watcher in w.iter_watchers():
            self.assertGreaterEqual(watcher.runs, 1)

        w.reset()
        for watcher in w.iter_watchers():
            self.assertEqual(watcher.runs, 0)
            self.assertEqual(watcher.successes, 0)
            self.assertEqual(watcher.failures, 0)
            self.assertEqual(watcher.elapsed, 0)
            self.assertEqual(watcher.elapsed_waiting, 0)

    def test_worker(self):
        called = []

        def cb():
            called.append(1)

        callables = [
            (every_one_sec, (cb,), None),
            (every_half_sec, (cb,), None),
        ]
        executor_factory = lambda: self.executor_cls(**self.executor_kwargs)
        w = periodics.PeriodicWorker(callables,
                                     executor_factory=executor_factory,
                                     **self.worker_kwargs)
        with self.create_destroy(w.start):
            self.sleep(2.0)
            w.stop()

        am_called = sum(called)
        self.assertGreaterEqual(am_called, 4)
