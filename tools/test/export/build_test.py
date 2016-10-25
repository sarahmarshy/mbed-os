#!/usr/bin/env python
"""
mbed SDK
Copyright (c) 2011-2016 ARM Limited

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import sys
from os import remove, rename
from os.path import join, dirname, exists, abspath
ROOT = abspath(join(dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)
import argparse
from argparse import ArgumentTypeError
import sys
from shutil import rmtree
from collections import namedtuple
from copy import copy
import zipfile


from tools.paths import EXPORT_DIR
from tools.tests import TESTS
from tools.build_api import get_mbed_official_release, RELEASE_VERSIONS
from tools.test_api import find_tests
from tools.project import export
from Queue import Queue
from threading import Thread, Lock
from tools.project_api import print_results, get_exporter_toolchain
from tools.tests import test_name_known, test_known, Test
from tools.export.exporters import FailedBuildException, \
                                   TargetNotSupportedException
from tools.utils import argparse_force_lowercase_type, \
                        argparse_many, columnate, args_error, \
                        argparse_filestring_type
from tools.options import extract_profile

print_lock = Lock()

def do_queue(Class, function, interable) :
    q = Queue()
    threads = [Class(q, function) for each in range(20)]
    for thing in interable :
        q.put(thing)
    for each in threads :
        each.setDaemon(True)
        each.start()
    q.join()


class Reader (Thread) :
    def __init__(self, queue, func) :
        Thread.__init__(self)
        self.queue = queue
        self.func = func

    def start(self) :
        sys.stdout.flush()
        while not self.queue.empty() :
            test = self.queue.get()
            self.func(test)
            self.queue.task_done()


class ExportBuildTest(object):
    """Object to encapsulate logic for progen build testing"""
    def __init__(self, tests, parser, options):
        """
        Initialize an instance of class ProgenBuildTest
        Args:
            tests: array of TestCase instances
        """
        self.total = len(tests)
        self.parser = parser
        self.options = options
        self.counter = 0
        self.successes = []
        self.failures = []
        self.skips = []
        self.tests = [ExportBuildTest.test_case(test) for test in tests]
        self.build_queue = Queue()

    @staticmethod
    def test_case(case):
        TestCase = namedtuple('TestCase', case.keys())
        return TestCase(**case)

    def handle_log(self,log):
        print log
        try:
            with open(log, 'r') as in_log:
                print in_log.read()
                sys.stdout.flush()
            log_name = join(EXPORT_DIR, dirname(log) + "_log.txt")
            if exists(log_name):
                # delete it if so
                remove(log_name)
            rename(log, log_name)
        except IOError:
            pass

    def batch_tests(self, clean=False):
        """Performs all exports of self.tests
        Peroform_exports will fill self.build_queue.
        This function will empty self.build_queue and call the test's
        IDE's build function."""
        do_queue(Reader, self.perform_exports, self.tests)
        self.counter = 0
        self.total = self.build_queue.qsize()
        while not self.build_queue.empty():
            build = self.build_queue.get()
            self.counter +=1
            exporter = build[0]
            test_case = build[1]
            self.display_counter("Building test case  %s::%s\t%s"
                                 % (test_case.mcu,
                                    test_case.ide,
                                    test_case.name))
            try:
                if test_case.zip:
                    name_str = ('%s_%s_%s') % (test_case.mcu, test_case.ide, test_case.name)
                    with zipfile.ZipFile(exporter.zipfile, "r") as zip_file:
                        zip_file.extractall(path=join(EXPORT_DIR, name_str))
                exporter.build()
            except FailedBuildException:
                self.failures.append("%s::%s\t%s" % (test_case.mcu,
                                                     test_case.ide,
                                                     test_case.name))
            else:
                self.successes.append("%s::%s\t%s" % (test_case.mcu,
                                                      test_case.ide,
                                                      test_case.name))
            self.handle_log(exporter.generated_files[-1])
            if clean:
                rmtree(exporter.export_dir)

    def display_counter (self, message) :
        with print_lock:
            sys.stdout.write("{}/{} {}".format(self.counter, self.total,
                                               message) +"\n")
            sys.stdout.flush()

    def perform_exports(self, test_case):
        """
        Generate the project file for test_case and fill self.build_queue
        Args:
            test_case: object of type TestCase
        """
        sys.stdout.flush()
        self.counter += 1
        name_str = ('%s_%s_%s') % (test_case.mcu, test_case.ide, test_case.name)
        self.display_counter("Exporting test case  %s::%s\t%s" % (test_case.mcu,
                                                                  test_case.ide,
                                                                  test_case.name))

        try:
            _, toolchain = get_exporter_toolchain(test_case.ide)
            profile = extract_profile(self.parser, self.options, toolchain)
            zip_name = name_str+'.zip' if test_case.zip else None
            exporter = export(test_case.mcu, test_case.ide,
                              project_id=test_case.id, zip_proj=zip_name,
                              clean=True, src=test_case.src,
                              export_path=join(EXPORT_DIR,name_str),
                              silent=True, build_profile=profile)
            exporter.generated_files.append(join(EXPORT_DIR,name_str,test_case.log))
            self.build_queue.put((exporter,test_case))
        except TargetNotSupportedException:
            self.skips.append("%s::%s\t%s" % (test_case.mcu, test_case.ide,
                                              test_case.name))
            # Check if the specified name is in all_os_tests


def check_valid_mbed_os(test):
    """Check if the specified name is in all_os_tests
    args:
        test: string name to index all_os_tests
    returns: tuple of test_name and source location of test,
        as given by find_tests"""
    all_os_tests = find_tests(ROOT, "K64F", "ARM")
    if test in all_os_tests.keys():
        return (test, all_os_tests[test])
    else:
        supported = columnate([t for t in all_os_tests.keys()])
        raise ArgumentTypeError("Program with name '{0}' not found. "
                                "Supported tests are: \n{1}".format(test,supported))


def check_version(version):
    """Check if the specified version is valid
    args:
        version: integer versio of mbed
    returns:
        version if it is valid"""
    if version not in RELEASE_VERSIONS:
        raise ArgumentTypeError("Choose from versions : %s"%", ".join(RELEASE_VERSIONS))
    return version


def main():
    """Entry point"""

    ide_list = ["iar", "uvision", "make_gcc_arm", "make_iar", "make_armc5"]

    default_v2 = [test_name_known("MBED_BLINKY")]
    default_v5 = [check_valid_mbed_os('tests-mbedmicro-rtos-mbed-basic')]

    parser = argparse.ArgumentParser(description=
                                     "Test progen builders. Leave any flag off"
                                     " to run with all possible options.")
    parser.add_argument("-i",
                        dest="ides",
                        default=ide_list,
                        type=argparse_many(argparse_force_lowercase_type(
                            ide_list, "toolchain")),
                        help="The target IDE: %s"% str(ide_list))

    parser.add_argument( "-p",
                        type=argparse_many(test_known),
                        dest="programs",
                        help="The index of the desired test program: [0-%d]"
                             % (len(TESTS) - 1))

    parser.add_argument("-n",
                        type=argparse_many(test_name_known),
                        dest="programs",
                        help="The name of the desired test program")

    parser.add_argument("-m", "--mcu",
                        help=("Generate projects for the given MCUs"),
                        metavar="MCU",
                        type=argparse_many(str.upper))

    parser.add_argument("-os-tests",
                        type=argparse_many(check_valid_mbed_os),
                        dest="os_tests",
                        help="Mbed-os tests")

    parser.add_argument("-c", "--clean",
                        dest="clean",
                        action="store_true",
                        help="clean up the exported project files",
                        default=False)

    parser.add_argument("--release",
                        dest="release",
                        type=check_version,
                        help="Which version of mbed to test",
                        default=RELEASE_VERSIONS[-1])

    parser.add_argument("--profile",
                        dest="profile",
                        action="append",
                        type=argparse_filestring_type,
                        default=[])

    options = parser.parse_args()
    # targets in chosen release
    targetnames = [target[0] for target in
                   get_mbed_official_release(options.release)]
    # all targets in release are default
    test_targets = options.mcu or targetnames
    if not all([t in targetnames for t in test_targets]):
        args_error(parser, "Only specify targets in release %s:\n%s"
                   %(options.release, columnate(targetnames)))

    v2_tests, v5_tests = [],[]
    if options.release == '5':
        v5_tests = options.os_tests or default_v5
    elif options.release == '2':
        v2_tests = options.programs or default_v2

    tests = []
    default_test = {key:None for key in ['ide', 'mcu', 'name', 'id', 'src', 'log', 'zip']}
    for mcu in test_targets:
        for ide in options.ides:
            log = "build_log.txt" if ide != 'uvision' \
                else join('build', 'build_log.txt')
            # add each test case to the tests array
            zip = True if "make" in ide else False
            default_test.update({'mcu': mcu, 'ide': ide, 'log':log, 'zip':zip})
            for test in v2_tests:
                default_test.update({'name':TESTS[test]["id"], 'id':test})
                tests.append(copy(default_test))
            for test in v5_tests:
                default_test.update({'name':test[0],'src':[test[1],ROOT]})
                tests.append(copy(default_test))
    test = ExportBuildTest(tests, parser, options)
    test.batch_tests(clean=options.clean)
    print_results(test.successes, test.failures, test.skips)
    sys.exit(len(test.failures))

if __name__ == "__main__":
    main()