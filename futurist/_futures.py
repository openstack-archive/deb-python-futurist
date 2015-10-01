# -*- coding: utf-8 -*-

#    Copyright (C) 2014 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import functools
import sys
import threading

from concurrent import futures as _futures
from concurrent.futures import process as _process
from concurrent.futures import thread as _thread
import six

try:
    from eventlet import greenpool
    from eventlet import patcher as greenpatcher
    from eventlet import queue as greenqueue

    from eventlet.green import threading as greenthreading
except ImportError:
    greenpatcher, greenpool, greenqueue, greenthreading = (None, None,
                                                           None, None)

from futurist import _utils


class RejectedSubmission(Exception):
    """Exception raised when a submitted call is rejected (for some reason)."""


# NOTE(harlowja): Allows for simpler access to this type...
Future = _futures.Future


class _Threading(object):

    @staticmethod
    def event_object(*args, **kwargs):
        return threading.Event(*args, **kwargs)

    @staticmethod
    def lock_object(*args, **kwargs):
        return threading.Lock(*args, **kwargs)

    @staticmethod
    def rlock_object(*args, **kwargs):
        return threading.RLock(*args, **kwargs)

    @staticmethod
    def condition_object(*args, **kwargs):
        return threading.Condition(*args, **kwargs)


class _Gatherer(object):
    def __init__(self, submit_func, lock_factory, start_before_submit=False):
        self._submit_func = submit_func
        self._stats_lock = lock_factory()
        self._stats = ExecutorStatistics()
        self._start_before_submit = start_before_submit

    @property
    def statistics(self):
        return self._stats

    def clear(self):
        with self._stats_lock:
            self._stats = ExecutorStatistics()

    def _capture_stats(self, started_at, fut):
        """Capture statistics

        :param started_at: when the activity the future has performed
                           was started at
        :param fut: future object
        """
        # If time somehow goes backwards, make sure we cap it at 0.0 instead
        # of having negative elapsed time...
        elapsed = max(0.0, _utils.now() - started_at)
        with self._stats_lock:
            # Use a new collection and lock so that all mutations are seen as
            # atomic and not overlapping and corrupting with other
            # mutations (the clone ensures that others reading the current
            # values will not see a mutated/corrupted one). Since futures may
            # be completed by different threads we need to be extra careful to
            # gather this data in a way that is thread-safe...
            (failures, executed, runtime, cancelled) = (self._stats.failures,
                                                        self._stats.executed,
                                                        self._stats.runtime,
                                                        self._stats.cancelled)
            if fut.cancelled():
                cancelled += 1
            else:
                executed += 1
                if fut.exception() is not None:
                    failures += 1
                runtime += elapsed
            self._stats = ExecutorStatistics(failures=failures,
                                             executed=executed,
                                             runtime=runtime,
                                             cancelled=cancelled)

    def submit(self, fn, *args, **kwargs):
        """Submit work to be executed and capture statistics."""
        if self._start_before_submit:
            started_at = _utils.now()
        fut = self._submit_func(fn, *args, **kwargs)
        if not self._start_before_submit:
            started_at = _utils.now()
        fut.add_done_callback(functools.partial(self._capture_stats,
                                                started_at))
        return fut


class ThreadPoolExecutor(_thread.ThreadPoolExecutor):
    """Executor that uses a thread pool to execute calls asynchronously.

    It gathers statistics about the submissions executed for post-analysis...

    See: https://docs.python.org/dev/library/concurrent.futures.html
    """

    threading = _Threading()

    def __init__(self, max_workers=None, check_and_reject=None):
        """Initializes a thread pool executor.

        :param max_workers: maximum number of workers that can be
                            simulatenously active at the same time, further
                            submitted work will be queued up when this limit
                            is reached.
        :type max_workers: int
        :param check_and_reject: a callback function that will be provided
                                 two position arguments, the first argument
                                 will be this executor instance, and the second
                                 will be the number of currently queued work
                                 items in this executors backlog; the callback
                                 should raise a :py:class:`.RejectedSubmission`
                                 exception if it wants to have this submission
                                 rejected.
        :type check_and_reject: callback
        """
        if max_workers is None:
            max_workers = _utils.get_optimal_thread_count()
        super(ThreadPoolExecutor, self).__init__(max_workers=max_workers)
        if self._max_workers <= 0:
            raise ValueError("Max workers must be greater than zero")
        # NOTE(harlowja): this replaces the parent classes non-reentrant lock
        # with a reentrant lock so that we can correctly call into the check
        # and reject lock, and that it will block correctly if another
        # submit call is done during that...
        self._shutdown_lock = threading.RLock()
        self._check_and_reject = check_and_reject or (lambda e, waiting: None)
        self._gatherer = _Gatherer(
            # Since our submit will use this gatherer we have to reference
            # the parent submit, bound to this instance (which is what we
            # really want to use anyway).
            super(ThreadPoolExecutor, self).submit,
            self.threading.lock_object)

    @property
    def statistics(self):
        """:class:`.ExecutorStatistics` about the executors executions."""
        return self._gatherer.statistics

    @property
    def alive(self):
        """Accessor to determine if the executor is alive/active."""
        return not self._shutdown

    def submit(self, fn, *args, **kwargs):
        """Submit some work to be executed (and gather statistics)."""
        with self._shutdown_lock:
            if self._shutdown:
                raise RuntimeError('Can not schedule new futures'
                                   ' after being shutdown')
            self._check_and_reject(self, self._work_queue.qsize())
            return self._gatherer.submit(fn, *args, **kwargs)


class ProcessPoolExecutor(_process.ProcessPoolExecutor):
    """Executor that uses a process pool to execute calls asynchronously.

    It gathers statistics about the submissions executed for post-analysis...

    See: https://docs.python.org/dev/library/concurrent.futures.html
    """

    threading = _Threading()

    def __init__(self, max_workers=None):
        if max_workers is None:
            max_workers = _utils.get_optimal_thread_count()
        super(ProcessPoolExecutor, self).__init__(max_workers=max_workers)
        if self._max_workers <= 0:
            raise ValueError("Max workers must be greater than zero")
        self._gatherer = _Gatherer(
            # Since our submit will use this gatherer we have to reference
            # the parent submit, bound to this instance (which is what we
            # really want to use anyway).
            super(ProcessPoolExecutor, self).submit,
            self.threading.lock_object)

    @property
    def alive(self):
        """Accessor to determine if the executor is alive/active."""
        return not self._shutdown_thread

    @property
    def statistics(self):
        """:class:`.ExecutorStatistics` about the executors executions."""
        return self._gatherer.statistics

    def submit(self, fn, *args, **kwargs):
        """Submit some work to be executed (and gather statistics)."""
        return self._gatherer.submit(fn, *args, **kwargs)


class _WorkItem(object):
    def __init__(self, future, fn, args, kwargs):
        self.future = future
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        if not self.future.set_running_or_notify_cancel():
            return
        try:
            result = self.fn(*self.args, **self.kwargs)
        except BaseException:
            exc_type, exc_value, exc_tb = sys.exc_info()
            try:
                if six.PY2:
                    self.future.set_exception_info(exc_value, exc_tb)
                else:
                    self.future.set_exception(exc_value)
            finally:
                del(exc_type, exc_value, exc_tb)
        else:
            self.future.set_result(result)


if _utils.EVENTLET_AVAILABLE:

    class _GreenThreading(object):

        @staticmethod
        def event_object(*args, **kwargs):
            return greenthreading.Event(*args, **kwargs)

        @staticmethod
        def lock_object(*args, **kwargs):
            return greenthreading.Lock(*args, **kwargs)

        @staticmethod
        def rlock_object(*args, **kwargs):
            return greenthreading.RLock(*args, **kwargs)

        @staticmethod
        def condition_object(*args, **kwargs):
            return greenthreading.Condition(*args, **kwargs)

    _green_threading = _GreenThreading()
else:
    _green_threading = None


class SynchronousExecutor(_futures.Executor):
    """Executor that uses the caller to execute calls synchronously.

    This provides an interface to a caller that looks like an executor but
    will execute the calls inside the caller thread instead of executing it
    in a external process/thread for when this type of functionality is
    useful to provide...

    It gathers statistics about the submissions executed for post-analysis...
    """

    threading = _Threading()

    def __init__(self, green=False):
        """Synchronous executor constructor.

        :param green: when enabled this forces the usage of greened lock
                      classes and green futures (so that the internals of this
                      object operate correctly under eventlet)
        :type green: bool
        """
        if green and not _utils.EVENTLET_AVAILABLE:
            raise RuntimeError('Eventlet is needed to use a green'
                               ' synchronous executor')
        self._shutoff = False
        if green:
            self.threading = _green_threading
            self._future_cls = GreenFuture
        else:
            self._future_cls = Future
        self._gatherer = _Gatherer(self._submit,
                                   self.threading.lock_object,
                                   start_before_submit=True)

    @property
    def alive(self):
        """Accessor to determine if the executor is alive/active."""
        return not self._shutoff

    def shutdown(self, wait=True):
        self._shutoff = True

    def restart(self):
        """Restarts this executor (*iff* previously shutoff/shutdown).

        NOTE(harlowja): clears any previously gathered statistics.
        """
        if self._shutoff:
            self._shutoff = False
            self._gatherer.clear()

    @property
    def statistics(self):
        """:class:`.ExecutorStatistics` about the executors executions."""
        return self._gatherer.statistics

    def submit(self, fn, *args, **kwargs):
        """Submit some work to be executed (and gather statistics)."""
        if self._shutoff:
            raise RuntimeError('Can not schedule new futures'
                               ' after being shutdown')
        return self._gatherer.submit(fn, *args, **kwargs)

    def _submit(self, fn, *args, **kwargs):
        fut = self._future_cls()
        runner = _WorkItem(fut, fn, args, kwargs)
        runner.run()
        return fut


class _GreenWorker(object):
    def __init__(self, executor, work, work_queue):
        self.executor = executor
        self.work = work
        self.work_queue = work_queue

    def __call__(self):
        # Run our main piece of work.
        try:
            self.work.run()
        finally:
            # Consume any delayed work before finishing (this is how we finish
            # work that was to big for the pool size, but needs to be finished
            # no matter).
            while True:
                try:
                    w = self.work_queue.get_nowait()
                except greenqueue.Empty:
                    break
                else:
                    try:
                        w.run()
                    finally:
                        self.work_queue.task_done()


class GreenFuture(Future):
    __doc__ = Future.__doc__

    def __init__(self):
        super(GreenFuture, self).__init__()
        if not _utils.EVENTLET_AVAILABLE:
            raise RuntimeError('Eventlet is needed to use a green future')
        # NOTE(harlowja): replace the built-in condition with a greenthread
        # compatible one so that when getting the result of this future the
        # functions will correctly yield to eventlet. If this is not done then
        # waiting on the future never actually causes the greenthreads to run
        # and thus you wait for infinity.
        if not greenpatcher.is_monkey_patched('threading'):
            self._condition = greenthreading.Condition()


class GreenThreadPoolExecutor(_futures.Executor):
    """Executor that uses a green thread pool to execute calls asynchronously.

    See: https://docs.python.org/dev/library/concurrent.futures.html
    and http://eventlet.net/doc/modules/greenpool.html for information on
    how this works.

    It gathers statistics about the submissions executed for post-analysis...
    """

    threading = _green_threading

    def __init__(self, max_workers=1000, check_and_reject=None):
        """Initializes a green thread pool executor.

        :param max_workers: maximum number of workers that can be
                            simulatenously active at the same time, further
                            submitted work will be queued up when this limit
                            is reached.
        :type max_workers: int
        :param check_and_reject: a callback function that will be provided
                                 two position arguments, the first argument
                                 will be this executor instance, and the second
                                 will be the number of currently queued work
                                 items in this executors backlog; the callback
                                 should raise a :py:class:`.RejectedSubmission`
                                 exception if it wants to have this submission
                                 rejected.
        :type check_and_reject: callback
        """
        if not _utils.EVENTLET_AVAILABLE:
            raise RuntimeError('Eventlet is needed to use a green executor')
        if max_workers <= 0:
            raise ValueError("Max workers must be greater than zero")
        self._max_workers = max_workers
        self._pool = greenpool.GreenPool(self._max_workers)
        self._delayed_work = greenqueue.Queue()
        self._check_and_reject = check_and_reject or (lambda e, waiting: None)
        self._shutdown_lock = greenthreading.Lock()
        self._shutdown = False
        self._gatherer = _Gatherer(self._submit,
                                   self.threading.lock_object)

    @property
    def alive(self):
        """Accessor to determine if the executor is alive/active."""
        return not self._shutdown

    @property
    def statistics(self):
        """:class:`.ExecutorStatistics` about the executors executions."""
        return self._gatherer.statistics

    def submit(self, fn, *args, **kwargs):
        """Submit some work to be executed (and gather statistics).

        :param args: non-keyworded arguments
        :type args: list
        :param kwargs: key-value arguments
        :type kwargs: dictionary
        """
        with self._shutdown_lock:
            if self._shutdown:
                raise RuntimeError('Can not schedule new futures'
                                   ' after being shutdown')
            self._check_and_reject(self, self._delayed_work.qsize())
            return self._gatherer.submit(fn, *args, **kwargs)

    def _submit(self, fn, *args, **kwargs):
        f = GreenFuture()
        work = _WorkItem(f, fn, args, kwargs)
        if not self._spin_up(work):
            self._delayed_work.put(work)
        return f

    def _spin_up(self, work):
        """Spin up a greenworker if less than max_workers.

        :param work: work to be given to the greenworker
        :returns: whether a green worker was spun up or not
        :rtype: boolean
        """
        alive = self._pool.running() + self._pool.waiting()
        if alive < self._max_workers:
            self._pool.spawn_n(_GreenWorker(self, work, self._delayed_work))
            return True
        return False

    def shutdown(self, wait=True):
        with self._shutdown_lock:
            if not self._shutdown:
                self._shutdown = True
                shutoff = True
            else:
                shutoff = False
        if wait and shutoff:
            self._pool.waitall()
            self._delayed_work.join()


class ExecutorStatistics(object):
    """Holds *immutable* information about a executors executions."""

    __slots__ = ['_failures', '_executed', '_runtime', '_cancelled']

    __repr_format = ("failures=%(failures)s, executed=%(executed)s, "
                     "runtime=%(runtime)s, cancelled=%(cancelled)s")

    def __init__(self, failures=0, executed=0, runtime=0.0, cancelled=0):
        self._failures = failures
        self._executed = executed
        self._runtime = runtime
        self._cancelled = cancelled

    @property
    def failures(self):
        """How many submissions ended up raising exceptions.

        :returns: how many submissions ended up raising exceptions
        :rtype: number
        """
        return self._failures

    @property
    def executed(self):
        """How many submissions were executed (failed or not).

        :returns: how many submissions were executed
        :rtype: number
        """
        return self._executed

    @property
    def runtime(self):
        """Total runtime of all submissions executed (failed or not).

        :returns: total runtime of all submissions executed
        :rtype: number
        """
        return self._runtime

    @property
    def cancelled(self):
        """How many submissions were cancelled before executing.

        :returns: how many submissions were cancelled before executing
        :rtype: number
        """
        return self._cancelled

    @property
    def average_runtime(self):
        """The average runtime of all submissions executed.

        :returns: average runtime of all submissions executed
        :rtype: number
        :raises: ZeroDivisionError when no executions have occurred.
        """
        return self._runtime / self._executed

    def __repr__(self):
        r = self.__class__.__name__
        r += "("
        r += self.__repr_format % ({
            'failures': self._failures,
            'executed': self._executed,
            'runtime': self._runtime,
            'cancelled': self._cancelled,
        })
        r += ")"
        return r
