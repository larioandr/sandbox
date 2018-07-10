#!/usr/bin/env python
#
# Smoke test tool for the INET Framework: checks that simulations don't crash
# or stop with a runtime error when run for a reasonable (CPU) time.
#
# Accepts one or more CSV files with (at least) two columns: working directory,
# and options to opp_run. More than two columns are accepted so that the
# tool can share the same input files with the fingerprint tester.
# The program runs the INET simulations in the CSV files, and reports crashes
# and runtime errors as FAILs.
#
# Implementation is based on Python's unit testing library, so it can be
# integrated into larger test suites with minimal effort.
#
# Shares a fair amount of code with the fingerprint tester; those parts
# should sometime be factored out as a Python module.
#
# Author: Andras Varga
#

import argparse
import copy
import csv
import glob
import multiprocessing
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from distutils import spawn
from io import StringIO


SIMROOT = os.path.abspath("../..")
PATH_SEPARATOR = ";" if sys.platform == 'win32' else ':'
NED_PATH = PATH_SEPARATOR.join([".", ])
EXECUTABLE = "opp_run"
DEFAULT_CPU_TIME_LIMIT = "6s"
DEFAULT_LOG_FILE = "test.out"
DEFAULT_MEMCHECK = False
MEMCHECK_FAILED_ERROR_CODE = 66


class SmokeTestCaseGenerator():
    fileToSimulationsMap = {}

    def generateFromCSV(self, csvFileList, filterRegexList, excludeFilterRegexList, repeat):
        testcases = []
        for csvFile in csvFileList:
            simulations = self.parseSimulationsTable(csvFile)
            self.fileToSimulationsMap[csvFile] = simulations
            testcases.extend(self.generateFromDictList(simulations, filterRegexList, excludeFilterRegexList, repeat))
        return testcases

    def generateFromDictList(self, simulations, filterRegexList, excludeFilterRegexList, repeat):
        class StoreFingerprintCallback:
            def __init__(self, simulation):
                self.simulation = simulation
            def __call__(self, fingerprint):
                self.simulation['computedFingerprint'] = fingerprint

        class StoreExitcodeCallback:
            def __init__(self, simulation):
                self.simulation = simulation
            def __call__(self, exitcode):
                self.simulation['exitcode'] = exitcode

        testcases = []
        for simulation in simulations:
            title = simulation['wd'] + " " + simulation['args']
            if not filterRegexList or ['x' for regex in filterRegexList if re.search(regex, title)]: # if any regex matches title
                if not excludeFilterRegexList or not ['x' for regex in excludeFilterRegexList if re.search(regex, title)]: # if NO exclude-regex matches title
                    testcases.append(SmokeTestCase(title, simulation['file'], simulation['wd'], simulation['args']))
        return testcases

    def commentRemover(self, csvData):
        comment_regex = re.compile(' *#.*$')
        endline_regex = re.compile('\\n')
        for line in csvData:
            line = comment_regex.sub('', line)
            line = endline_regex.sub('', line)
            yield line

    # parse the CSV into a list of dicts
    def parseSimulationsTable(self, csvFile):
        simulations = []
        f = open(csvFile, 'r')
        csvReader = csv.reader(self.commentRemover(f), delimiter=',', quotechar='"', skipinitialspace=True)
        for fields in csvReader:
            if len(fields) == 0:
                continue        # empty line
            if len(fields) != 2:
                raise Exception("Line " + str(csvReader.line_num) + " must contain 2 items, but contains " + str(len(fields)) + ": " + '"' + '", "'.join(fields) + '"')
            simulations.append({'file': csvFile, 'line' : csvReader.line_num, 'wd': fields[0], 'args': fields[1]})
        f.close()
        return simulations


class SimulationResult:
    def __init__(self, command, workingdir, exitcode, errorMsg=None, isFingerprintOK=None,
            computedFingerprint=None, simulatedTime=None, numEvents=None, elapsedTime=None, cpuTimeLimitReached=None):
        self.command = command
        self.workingdir = workingdir
        self.exitcode = exitcode
        self.errorMsg = errorMsg
        self.isFingerprintOK = isFingerprintOK
        self.computedFingerprint = computedFingerprint
        self.simulatedTime = simulatedTime
        self.numEvents = numEvents
        self.elapsedTime = elapsedTime
        self.cpuTimeLimitReached = cpuTimeLimitReached


class SimulationTestCase(unittest.TestCase):
    def runSimulation(self, title, command, workingdir):
        global logFile

        # run the program and log the output
        t0 = time.time()

        print('===========')
        print('command: ', command)
        print('workdir: ', workingdir)
        print('===========')

        (exitcode, out) = self.runProgram(command, workingdir)

        elapsedTime = time.time() - t0
        FILE = open(logFile, "a")
        FILE.write("------------------------------------------------------\n"
                 + "Running: " + title + "\n\n"
                 + "$ cd " + workingdir + "\n"
                 + "$ " + command + "\n\n"
                 + out.strip() + "\n\n"
                 + "Exit code: " + str(exitcode) + "\n"
                 + "Elapsed time:  " + str(round(elapsedTime,2)) + "s\n\n")
        FILE.close()

        result = SimulationResult(command, workingdir, exitcode, elapsedTime=elapsedTime)

        # process error messages
        errorLines = re.findall("<!>.*", out, re.M)
        errorMsg = ""
        for err in errorLines:
            err = err.strip()
            if re.search("Fingerprint", err):
                if re.search("successfully", err):
                    result.isFingerprintOK = True
                else:
                    m = re.search("(computed|calculated): ([-a-zA-Z0-9]+)", err)
                    if m:
                        result.isFingerprintOK = False
                        result.computedFingerprint = m.group(2)
                    else:
                        raise Exception("Cannot parse fingerprint-related error message: " + err)
            else:
                errorMsg += "\n" + err
                if re.search("CPU time limit reached", err):
                    result.cpuTimeLimitReached = True
                m = re.search("at event #([0-9]+), t=([0-9]*(\\.[0-9]+)?)", err)
                if m:
                    result.numEvents = int(m.group(1))
                    result.simulatedTime = float(m.group(2))

        result.errormsg = errorMsg.strip()
        return result

    def runProgram(self, command, workingdir):
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=workingdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        out = process.communicate()[0].decode('utf-8')
        out = re.sub("\r", "", out)
        return (process.returncode, out)


class SmokeTestCase(SimulationTestCase):
    def __init__(self, title, csvFile, wd, args):
        SimulationTestCase.__init__(self)
        self.title = title
        self.csvFile = csvFile
        self.wd = wd
        self.args = args

    def runTest(self):
        global SIMROOT, opp_run, NED_PATH, cpuTimeLimit, memcheck

        # run the simulation
        print('* selecting working directory: ')
        print('  self.wd = ', self.wd)
        print('  SIMROOT = ', SIMROOT)
        workingdir = _iif(self.wd.startswith('/'), SIMROOT + "/" + self.wd, self.wd)
        resultdir = os.path.abspath(".") + "/results/";
        if not os.path.exists(resultdir):
            os.makedirs(resultdir)
        memcheckPrefix = ""
        if memcheck:
            memcheckPrefix = "valgrind --leak-check=full --track-origins=yes --error-exitcode=" + str(MEMCHECK_FAILED_ERROR_CODE) + " --suppressions=" + SIMROOT + "/tests/smoke/valgrind.supp "
        command = memcheckPrefix + EXECUTABLE + " -n " + NED_PATH + " -u Cmdenv " + self.args + \
            " --cpu-time-limit=" + cpuTimeLimit + \
            " --vector-recording=false --scalar-recording=false" \
            " --cmdenv-express-mode=false" \
            " --result-dir=" + resultdir

        # print "COMMAND: " + command + '\n'
        result = self.runSimulation(self.title, command, workingdir)

        # process the result
        if result.exitcode == MEMCHECK_FAILED_ERROR_CODE:
            assert False, "memcheck detected errors"
        elif result.exitcode != 0:
            assert False, result.errormsg
        elif result.simulatedTime == 0:
            assert False, "zero time simulated"
        else:
            pass

    def __str__(self):
        return self.title


class ThreadSafeIter:
    """Takes an iterator/generator and makes it thread-safe by
    serializing call to the `next` method of given iterator/generator.
    """
    def __init__(self, it):
        self.it = it
        self.lock = threading.Lock()

    def __iter__(self):
        return self

    def __next__(self):
        with self.lock:
            return next(self.it)


class ThreadedTestSuite(unittest.BaseTestSuite):
    """ runs toplevel tests in n threads
    """

    # How many test process at the time.
    thread_count = multiprocessing.cpu_count()

    def run(self, result):
        it = ThreadSafeIter(self.__iter__())

        result.buffered = True

        threads = []

        for i in range(self.thread_count):
            # Create self.thread_count number of threads that together will
            # cooperate removing every ip in the list. Each thread will do the
            # job as fast as it can.
            t = threading.Thread(target=self.runThread, args=(result, it))
            t.daemon = True
            t.start()
            threads.append(t)

        # Wait until all the threads are done. .join() is blocking.
        #for t in threads:
        #    t.join()
        runApp = True
        while runApp and threading.active_count() > 1:
            try:
                time.sleep(0.1)
            except KeyboardInterrupt:
                runApp = False
        return result

    def runThread(self, result, it):
        tresult = result.startThread()
        for test in it:
            if result.shouldStop:
                break
            test(tresult)
        tresult.stopThread()


class ThreadedTestResult(unittest.TestResult):
    """TestResult with threads
    """

    def __init__(self, stream=None, descriptions=None, verbosity=None):
        super(ThreadedTestResult, self).__init__()
        self.parent = None
        self.lock = threading.Lock()

    def startThread(self):
        ret = copy.copy(self)
        ret.parent = self
        return ret

    def stop():
        super(ThreadedTestResult, self).stop()
        if self.parent:
            self.parent.stop()

    def stopThread(self):
        if self.parent == None:
            return 0
        self.parent.testsRun += self.testsRun
        return 1

    def startTest(self, test):
        "Called when the given test is about to be run"
        super(ThreadedTestResult, self).startTest(test)
        self.oldstream = self.stream
        self.stream = StringIO()

    def stopTest(self, test):
        """Called when the given test has been run"""
        super(ThreadedTestResult, self).stopTest(test)
        out = self.stream.getvalue()
        with self.lock:
            self.stream = self.oldstream
            self.stream.write(out)


#
# Copy/paste of TextTestResult, with minor modifications in the output:
# we want to print the error text after ERROR and FAIL, but we don't want
# to print stack traces.
#
class SimulationTextTestResult(ThreadedTestResult):
    """A test result class that can print formatted text results to a stream.
    Used by TextTestRunner.
    """
    PATH_SEPARATORarator1 = '=' * 70
    PATH_SEPARATORarator2 = '-' * 70

    def __init__(self, stream, descriptions, verbosity):
        super(SimulationTextTestResult, self).__init__()
        self.stream = stream
        self.showAll = verbosity > 1
        self.dots = verbosity == 1
        self.descriptions = descriptions

    def getDescription(self, test):
        doc_first_line = test.shortDescription()
        if self.descriptions and doc_first_line:
            return '\n'.join((str(test), doc_first_line))
        else:
            return str(test)

    def startTest(self, test):
        super(SimulationTextTestResult, self).startTest(test)
        if self.showAll:
            self.stream.write(self.getDescription(test))
            self.stream.write(" ... ")
            self.stream.flush()

    def addSuccess(self, test):
        super(SimulationTextTestResult, self).addSuccess(test)
        if self.showAll:
            self.stream.write(": PASS\n")
        elif self.dots:
            self.stream.write('.')
            self.stream.flush()

    def addError(self, test, err):
        # modified
        super(SimulationTextTestResult, self).addError(test, err)
        self.errors[-1] = (test, err[1])  # super class method inserts stack trace; we don't need that, so overwrite it
        if self.showAll:
            (cause, detail) = self._splitMsg(err[1])
            self.stream.write(": ERROR (%s)\n" % cause)
            if detail:
                self.stream.write(detail)
                self.stream.write("\n")
        elif self.dots:
            self.stream.write('E')
            self.stream.flush()

    def addFailure(self, test, err):
        # modified
        super(SimulationTextTestResult, self).addFailure(test, err)
        self.failures[-1] = (test, err[1])  # super class method inserts stack trace; we don't need that, so overwrite it
        if self.showAll:
            (cause, detail) = self._splitMsg(err[1])
            self.stream.write(": FAIL (%s)\n" % cause)
            if detail:
                self.stream.write(detail)
                self.stream.write("\n")
        elif self.dots:
            self.stream.write('F')
            self.stream.flush()

    def addSkip(self, test, reason):
        super(SimulationTextTestResult, self).addSkip(test, reason)
        if self.showAll:
            self.stream.write(": skipped {0!r}".format(reason))
            self.stream.write("\n")
        elif self.dots:
            self.stream.write("s")
            self.stream.flush()

    def addExpectedFailure(self, test, err):
        super(SimulationTextTestResult, self).addExpectedFailure(test, err)
        if self.showAll:
            self.stream.write(":FAIL (expected)\n")
        elif self.dots:
            self.stream.write("x")
            self.stream.flush()

    def addUnexpectedSuccess(self, test):
        super(SimulationTextTestResult, self).addUnexpectedSuccess(test)
        if self.showAll:
            self.stream.write(": PASS (unexpected)\n")
        elif self.dots:
            self.stream.write("u")
            self.stream.flush()

    def printErrors(self):
        # modified
        if self.dots or self.showAll:
            self.stream.write("\n")
        self.printErrorList('Errors', self.errors)
        self.printErrorList('Failures', self.failures)

    def printErrorList(self, flavour, errors):
        # modified
        if errors:
            self.stream.writeln("%s:" % flavour)
        for test, err in errors:
            self.stream.write("  %s (%s)\n" % (self.getDescription(test), self._splitMsg(err)[0]))

    def _splitMsg(self, msg):
        cause = str(msg)
        detail = None
        if cause.count(': '):
            (cause, detail) = cause.split(': ',1)
        return (cause, detail)


def _iif(cond,t,f):
    return t if cond else f

def ensure_dir(f):
    if not os.path.exists(f):
        os.makedirs(f)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Run the fingerprint tests specified in the input files.')
    parser.add_argument('testspecfiles', nargs='*', metavar='testspecfile', help='CSV files that contain the tests to run (default: *.csv). Expected CSV file columns: workingdir, args, simtimelimit, fingerprint')
    parser.add_argument('-m', '--match', action='append', metavar='regex', help='Line filter: a line (more precisely, workingdir+SPACE+args) must match any of the regular expressions in order for that test case to be run')
    parser.add_argument('-x', '--exclude', action='append', metavar='regex', help='Negative line filter: a line (more precisely, workingdir+SPACE+args) must NOT match any of the regular expressions in order for that test case to be run')
    parser.add_argument('-c', '--memcheck', default=DEFAULT_MEMCHECK, dest='memcheck', action='store_true', help='run using valgrind memcheck (default: false)')
    parser.add_argument('-s', '--cpu-time-limit', default=DEFAULT_CPU_TIME_LIMIT, dest='cpuTimeLimit', help='cpu time limit (default: 6s)')
    parser.add_argument('-t', '--threads', type=int, default=multiprocessing.cpu_count(), help='number of parallel threads (default: number of CPUs, currently '+str(multiprocessing.cpu_count())+')')
    parser.add_argument('-l', '--logfile', default=DEFAULT_LOG_FILE, dest='logFile', help='name of logfile (default: test.out)')
    parser.add_argument('-e', '--EXECUTABLE', help='Determines which binary to execute (e.g. opp_run_dbg, opp_run_release)')
    args = parser.parse_args()
    memcheck = args.memcheck
    cpuTimeLimit = args.cpuTimeLimit
    logFile = args.logFile

    if os.path.isfile(logFile):
        FILE = open(logFile, "w")
        FILE.close()

    if not args.testspecfiles:
        args.testspecfiles = glob.glob('*.csv')

    if args.EXECUTABLE:
        EXECUTABLE = args.EXECUTABLE

    generator = SmokeTestCaseGenerator()
    testcases = generator.generateFromCSV(args.testspecfiles, args.match, args.exclude, 1)

    testSuite = ThreadedTestSuite()
    testSuite.addTests(testcases)
    testSuite.thread_count = args.threads

    testRunner = unittest.TextTestRunner(stream=sys.stdout, verbosity=9, resultclass=SimulationTextTestResult)

    testRunner.run(testSuite)

    print('')
    print("Log has been saved to %s" % logFile)
