# coding=utf-8
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from logging import getLogger
from shutil import rmtree
from tempfile import mkdtemp
from time import sleep, time

from sapphire import SERVED_TIMEOUT
from ..target import TargetLaunchError, TargetLaunchTimeout
from .utils import grz_tmp

__all__ = ("Runner", "RunResult")
__author__ = "Tyson Smith"
__credits__ = ["Tyson Smith"]

LOG = getLogger(__name__)

# _IdleChecker is used to help determine if the target is hung (actively using CPU)
# or if it has not made expected the HTTP requests for other reasons (idle).
# This will allow the framework to move on without interrupting execution of
# long running test cases.
# This is not perfect! It is to be used AFTER the test case timeout
# (initial_delay) has elapsed.
class _IdleChecker(object):
    __slots__ = ("_check_cb", "_init_delay", "_poll_delay", "_threshold", "_next_poll")

    def __init__(self, check_cb, threshold, initial_delay, poll_delay=1):
        assert callable(check_cb)
        assert initial_delay >= 0
        assert poll_delay >= 0
        assert 100 > threshold >= 0
        self._check_cb = check_cb  # callback used to check if target is idle
        self._init_delay = initial_delay  # time to wait before the initial idle poll
        self._poll_delay = poll_delay  # time to wait between subsequent polls
        self._threshold = threshold  # CPU usage threshold
        self._next_poll = None

    def is_idle(self):
        """Check the target idle callback. This is throttled by '_next_poll'
        which specifies the time at which the next callback call is allowed.

        Args:
            None

        Returns:
            bool: True if the target idle callback returned True otherwise False
        """
        assert self._next_poll is not None, "schedule_poll() must be called first"
        now = time()
        if now >= self._next_poll:
            if self._check_cb(self._threshold):
                return True
            self.schedule_poll(now=now)
        return False

    def schedule_poll(self, initial=False, now=None):
        """Update `_next_poll`.

        Args:
            initial (int): Use `_init_delay` to schedule next poll
            now (int): time in seconds

        Returns:
            None
        """
        if now is None:
            now = time()
        if initial:
            self._next_poll = now + self._init_delay
        else:
            self._next_poll = now + self._poll_delay


class Runner(object):
    __slots__ = ("_idle", "_server", "_target", "_tests_run")

    def __init__(self, server, target, idle_threshold=0, idle_delay=0):
        if idle_threshold > 0:
            assert idle_delay > 0
            LOG.debug("using idle check, th %d, delay %ds", idle_threshold, idle_delay)
            self._idle = _IdleChecker(target.is_idle, idle_threshold, idle_delay)
        else:
            self._idle = None
        self._server = server  # a sapphire instance to serve the test case
        self._target = target  # target to run test case
        self._tests_run = 0  # number of tests run since target (re)launched

    def launch(self, location, env_mod=None, max_retries=3, retry_delay=0):
        """Launch a target and open `location`.

        Args:
            location (str): URL to open via Target.
            env_mod (dict): Environment modifications.
            max_retries (int): Number of retries to preform before re-raising
                               TargetLaunchTimeout.
            retry_delay (int): Time in seconds to wait between retries.

        Returns:
            None
        """
        assert self._server is not None
        assert self._target is not None
        assert max_retries >= 0
        assert retry_delay >= 0
        self._server.clear_backlog()
        LOG.debug("launching target (timeout %ds)", self._target.launch_timeout)
        for retries in reversed(range(max_retries)):
            try:
                self._target.launch(location, env_mod=env_mod)
            except TargetLaunchError as exc:
                # This is likely due to a bad build or environment configuration.
                if retries:
                    LOG.warning("Failure detected during launch (retries %d)", retries)
                    exc.report.cleanup()
                    sleep(retry_delay)
                    continue
                raise
            except TargetLaunchTimeout:
                # A TargetLaunchTimeout likely has nothing to do with Grizzly but is
                # seen frequently on machines under a high load. After multiple
                # consecutive timeouts something is likely wrong so raise.
                if retries:
                    LOG.warning("Timeout detected during launch (retries %d)", retries)
                    sleep(retry_delay)
                    continue
                raise
            break
        self._tests_run = 0

    @staticmethod
    def location(srv_path, srv_port, close_after=None, forced_close=True, timeout=None):
        """Build a valid URL to pass to a browser.

        Args:
            srv_path (str): Path segment of the URL
            srv_port (int): Server listening port
            close_after (int): Harness argument.
            forced_close (bool): Harness argument.
            timeout (int): Harness argument.

        Returns:
            str: A valid URL.
        """
        location = "http://127.0.0.1:%d/%s" % (srv_port, srv_path.lstrip("/"))
        # set harness related arguments
        args = []
        if close_after is not None:
            assert close_after >= 0
            args.append("close_after=%d" % (close_after,))
        if not forced_close:
            args.append("forced_close=0")
        if timeout is not None:
            assert timeout >= 0
            args.append("timeout=%d" % (timeout * 1000,))
        if args:
            return "?".join([location, "&".join(args)])
        return location

    def run(self, ignore, server_map, testcase, test_path=None, coverage=False, wait_for_callback=False):
        """Serve a testcase and monitor the target for results.

        Args:
            ignore (list): List of failure types to ignore.
            server_map (sapphire.ServerMap): A ServerMap.
            testcase (grizzly.TestCase): The test case that will be served.
            test_path (str): Location of test case data on the filesystem.
            coverage (bool): Trigger coverage dump.
            wait_for_callback: (bool): Use `_keep_waiting()` to indicate when
                                       framework should move on.

        Returns:
            RunResult: Files served, status and timeout flag from the run.
        """
        if self._idle is not None:
            self._idle.schedule_poll(initial=True)
        try:
            # unpack test case
            if test_path is None:
                wwwdir = mkdtemp(prefix="test_", dir=grz_tmp("serve"))
                testcase.dump(wwwdir)
            else:
                wwwdir = test_path
            # serve the test case
            serve_start = time()
            server_status, served = self._server.serve_path(
                wwwdir,
                continue_cb=self._keep_waiting,
                forever=wait_for_callback,
                optional_files=tuple(testcase.optional),
                server_map=server_map)
            duration = time() - serve_start
        finally:
            # remove temporary files
            if test_path is None:
                rmtree(wwwdir)
        result = RunResult(served, duration, timeout=server_status == SERVED_TIMEOUT)
        # TODO: fix calling TestCase.add_batch() for multi-test replay
        # add all include files that were served
        for url, resource in server_map.include.items():
            testcase.add_batch(resource.target, result.served, prefix=url)
        result.attempted = testcase.landing_page in result.served
        if result.attempted and coverage and not result.timeout:
            # dump_coverage() should be called before detect_failure()
            # to help catch any coverage related issues.
            self._target.dump_coverage()
        # detect failure
        failure_detected = self._target.detect_failure(ignore, result.timeout)
        result.initial = self._tests_run == 0
        if not result.attempted:
            # something is wrong so close the target
            # previous iteration put target in a bad state?
            LOG.debug("landing page %r not served!", testcase.landing_page)
            self._target.close()
        else:
            self._tests_run += 1
        if failure_detected == self._target.RESULT_FAILURE:
            result.status = RunResult.FAILED
        elif failure_detected == self._target.RESULT_IGNORED:
            result.status = RunResult.IGNORED
        return result

    def _keep_waiting(self):
        """Callback used by the server to determine if it should continue to
        wait for the requests from the target.

        Args:
            None

        Returns:
            bool: Continue to serve the test case.
        """
        if self._idle is not None and self._idle.is_idle():
            LOG.debug("idle target detected")
            return False
        return self._target.monitor.is_healthy()


class RunResult(object):
    FAILED = 1
    IGNORED = 2

    __slots__ = ("attempted", "duration", "initial", "served", "status", "timeout")

    def __init__(self, served, duration, status=None, timeout=False):
        self.attempted = False  # entry point/landing page was requested
        self.duration = duration
        self.initial = False  # target was (re)launched prior to attempt
        self.served = served
        self.status = status
        self.timeout = timeout
