# coding=utf-8
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from logging import getLogger
from os.path import isdir
from shutil import rmtree
from tempfile import mkdtemp
from time import sleep, time

from .common import grz_tmp, Report, Runner, RunResult, Status, TestFile


__all__ = ("SessionError", "LogOutputLimiter", "Session")
__author__ = "Tyson Smith"
__credits__ = ["Tyson Smith", "Jesse Schwartzentruber"]


log = getLogger(__name__)  # pylint: disable=invalid-name


class SessionError(Exception):
    """The base class for exceptions raised by Session"""


class LogOutputLimiter(object):
    __slots__ = ("_delay", "_iterations", "_launches", "_multiplier", "_time", "_verbose")

    def __init__(self, delay=300, delta_multiplier=2, verbose=False):
        self._delay = delay  # maximum time delay between output
        self._iterations = 1  # next iteration to trigger output
        self._launches = 1  # next launch to trigger output
        self._multiplier = delta_multiplier  # rate to decrease output (iterations)
        self._time = time()
        self._verbose = verbose  # always output

    def ready(self, cur_iter, launches):
        # calculate if a status line should be output
        if self._verbose:
            return True
        ready = False
        if cur_iter >= self._iterations:
            ready = True
            self._iterations *= self._multiplier
        elif launches >= self._launches:
            ready = True
        elif time() - self._delay >= self._time:
            ready = True
        if ready:
            self._time = time()
            self._launches = launches + 1
        return ready


class Session(object):
    DISPLAY_VERBOSE = 0  # display status every iteration
    DISPLAY_NORMAL = 1  # quickly reduce the amount of output
    EXIT_SUCCESS = 0
    EXIT_ERROR = 1
    EXIT_ARGS = 2
    EXIT_ABORT = 3
    EXIT_LAUNCH_FAILURE = 4
    EXIT_FAILURE = 5

    TARGET_LOG_SIZE_WARN = 0x1900000  # display warning when target log files exceed limit (25MB)

    __slots__ = ("adapter", "coverage", "iomanager", "reporter", "server", "status", "target")

    def __init__(self, adapter, iomanager, reporter, server, target, coverage=False):
        self.adapter = adapter
        self.coverage = coverage
        self.iomanager = iomanager
        self.reporter = reporter
        self.server = server
        self.status = Status.start()
        self.target = target

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.status.cleanup()

    def display_status(self, log_limiter):
        if self.adapter.remaining is not None:
            log.info(
                "[I%04d-L%02d-R%02d] %s",
                self.status.iteration,
                self.adapter.remaining,
                self.status.results,
                self.status.test_name)
        elif log_limiter.ready(self.status.iteration, self.target.monitor.launches):
            log.info("I%04d-R%02d ", self.status.iteration, self.status.results)

    def generate_testcase(self):
        log.debug("calling iomanager.create_testcase()")
        test = self.iomanager.create_testcase(self.adapter.NAME)
        log.debug("calling self.adapter.generate()")
        self.adapter.generate(test, self.iomanager.server_map)
        self.status.test_name = test.input_fname
        if self.target.prefs is not None:
            # TODO: this can likely be improved
            test.add_meta(TestFile.from_file(self.target.prefs, "prefs.js"))
        return test

    def report_result(self):
        # create working directory for target logs
        result_logs = mkdtemp(prefix="logs_", dir=grz_tmp("logs"))
        self.target.save_logs(result_logs)
        report = Report(result_logs, self.target.binary)
        short_sig = report.crash_info.createShortSignature()
        log.info("Result: %s (%s:%s)", short_sig, report.major[:8], report.minor[:8])
        # order test cases newest to oldest
        self.iomanager.tests.reverse()
        self.reporter.submit(self.iomanager.tests, report)
        if isdir(result_logs):
            rmtree(result_logs)
        self.status.count_result(short_sig)

    def run(self, ignore, iteration_limit=None, display_mode=DISPLAY_NORMAL):
        log_limiter = LogOutputLimiter(verbose=display_mode == self.DISPLAY_VERBOSE)
        runner = Runner(self.server, self.target)

        def _dyn_close():  # pragma: no cover
            if self.target.monitor.is_healthy():
                # delay to help catch window close/shutdown related crashes
                sleep(0.1)
                self.target.close()
            return b"<h1>Close Browser</h1>"
        self.iomanager.server_map.set_dynamic_response(
            "grz_close_browser",
            _dyn_close,
            mime_type="text/html")

        while True:
            self.status.report()
            self.status.iteration += 1

            if self.target.closed:
                # (re-)launch target
                self.iomanager.purge_tests()
                self.adapter.pre_launch()
                if self.iomanager.harness is None:
                    # harness is not in use, open the test case
                    location = runner.location(
                        "/grz_current_test",
                        self.server.port)
                else:
                    # harness is in use, open it and it will open the test case
                    location = runner.location(
                        "/grz_harness",
                        self.server.port,
                        close_after=self.target.rl_reset,
                        forced_close=self.target.forced_close,
                        timeout=self.adapter.TEST_DURATION)
                runner.launch(location, max_retries=3, retry_delay=0)
            self.target.step()

            # create and populate a test case
            current_test = self.generate_testcase()
            # display status
            self.display_status(log_limiter=log_limiter)

            # run test case
            result = runner.run(ignore, self.iomanager.server_map, current_test, coverage=self.coverage)
            current_test.duration = result.duration
            # adapter callbacks
            if result.timeout:
                log.debug("calling self.adapter.on_timeout()")
                self.adapter.on_timeout(current_test, result.served)
            else:
                log.debug("calling self.adapter.on_served()")
                self.adapter.on_served(current_test, result.served)
            # update test case
            if result.attempted:
                if not result.served:
                    # this can happen if the target crashes between serving test cases
                    log.info("Ignoring test case since nothing was served")
                    self.iomanager.tests.pop().cleanup()
                elif self.adapter.IGNORE_UNSERVED:
                    log.debug("removing unserved files from the test case")
                    current_test.purge_optional(result.served)
            else:
                log.error("Test case was not served")
                if not current_test.contains(current_test.landing_page):
                    log.warning("Test case is missing landing page")
                if result.initial:
                    # since this is the first iteration since the Target launched
                    # something is likely wrong with the Target or Adapter
                    err_logs = mkdtemp(prefix="error_", dir=grz_tmp("logs"))
                    self.target.save_logs(err_logs)
                    log.error("ERROR: Test case was not served. Timeout too short?")
                    log.error("Logs can be found here %r", err_logs)
                    raise SessionError("Please check Adapter and Target")

            # process results
            if result.status == RunResult.FAILED:
                log.debug("result detected")
                self.report_result()
            elif result.status == RunResult.IGNORED:
                self.status.ignored += 1
                log.info("Ignored (%d)", self.status.ignored)

            # trigger relaunch by closing the browser if needed
            self.target.check_relaunch()

            if self.adapter.remaining is not None and self.adapter.remaining < 1:
                # all test cases have been replayed
                log.info("Replay Complete")
                break

            if iteration_limit is not None and self.status.iteration == iteration_limit:
                log.info("Hit iteration limit")
                break

            # warn about large browser logs
            self.status.log_size = self.target.log_size()
            if self.status.log_size > self.TARGET_LOG_SIZE_WARN:
                log.warning("Large browser logs: %dMBs", (self.status.log_size / 0x100000))
