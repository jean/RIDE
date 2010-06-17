#  Copyright 2008-2009 Nokia Siemens Networks Oyj
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import os
import time
from threading import Thread

from robotide import context
from robotide.controller import DataController, ResourceFileController
from robotide.errors import DataError, SerializationError
from robotide.writer.serializer import Serializer
from robot.parsing.model import TestData, TestCaseFile, TestDataDirectory
from robotide.publish.messages import RideOpenResource, RideSaving, RideSaveAll,\
    RideSaved


class ChiefController(object):

    def __init__(self, namespace):
        self._namespace = namespace
        self._controller = None
        self.resources = []

    @property
    def data(self):
        return self._controller

    @property
    def suite(self):
        return self._controller.data if self._controller else None

    def load_data(self, load_observer, path):
        try:
            self.load_datafile(load_observer, path)
        except DataError:
            resource = self.load_resource(path)
            if not resource:
                raise DataError("Given file '%s' is not a valid Robot Framework "
                                "test case or resource file" % path)

    def load_datafile(self, load_observer, path):
        datafile = self._load_datafile(load_observer, path)
        resources = self._load_resources(datafile, load_observer)
        self._create_controllers(datafile, resources)
        load_observer.finished()

    def _load_datafile(self, load_observer, path):
        loader = _DataLoader(path)
        loader.start()
        while loader.isAlive():
            time.sleep(0.1)
            load_observer.notify()
        if not loader.datafile:
            raise DataError('Invalid data file: %s.' % path)
        return loader.datafile

    def _create_controllers(self, datafile, resources):
        self._controller = DataController(datafile)
        self.resources = [ResourceFileController(r) for r in resources]

    def _load_resources(self, datafile, load_observer):
        loader = _ResourceLoader(datafile, self._namespace.get_resources)
        loader.start()
        while loader.isAlive():
            time.sleep(0.1)
            load_observer.notify()
        return loader.resources

    def load_resource(self, path, datafile=None):
        resource = self._namespace.get_resource(path)
        if not resource:
            raise DataError('Invalid resource file: %s.' % path)
        controller = ResourceFileController(resource)
        RideOpenResource(path=resource.source).publish()
        if controller not in self.resources:
            self.resources.append(controller)
        return controller

    def _resolve_imported_resources(self, datafile):
        resources = datafile.get_resources()
        for res in resources:
            if res not in self.resources:
                self.resources.append(res)
        for item in datafile.suites + resources:
            self._resolve_imported_resources(item)

    def new_datafile(self, path):
        data = TestCaseFile()
        data.source = os.path.abspath(path)
        data.directory = os.path.dirname(data.source)
        self._create_missing_dirs(data.directory)
        self._create_controllers(data, [])

    def new_datadirectory(self, path):
        data = TestDataDirectory()
        path = os.path.abspath(path)
        data.source = os.path.dirname(path)
        data.directory = data.source
        data.initfile = path
        self._create_missing_dirs(data.directory)
        self._create_controllers(data, [])

    def _create_missing_dirs(self, dirpath):
        if not os.path.isdir(dirpath):
            os.makedirs(dirpath)

    def get_all_keywords(self):
        return self._namespace.get_all_keywords(ctrl.datafile for ctrl in self._get_all_controllers())

    def get_files_without_format(self, controller=None):
        if controller:
            controller_list = [controller]
        else:
            controller_list = self._get_all_dirty_controllers()
        return [ dc for dc in controller_list if dc.dirty and not dc.has_format() ]

    def get_root_suite_dir_path(self):
        return self.suite.get_dir_path()

    def is_directory_suite(self):
        return self.suite.is_directory_suite

    def is_dirty(self):
        if self.data and self._is_datafile_dirty(self.data):
            return True
        for res in self.resources:
            if res.dirty:
                return True
        return False

    def _is_datafile_dirty(self, datafile):
        if datafile.dirty:
            return True
        for df in datafile.children:
            if self._is_datafile_dirty(df):
                return True
        return False

    def save(self, controller):
        if controller:
            self.serialize_controller(controller)
        else:
            self.serialize_all()

    def serialize_all(self):
        errors = []
        datacontrollers = self._get_all_dirty_controllers()
        for dc in datacontrollers:
            try:
                self._serialize_file(dc)
            except SerializationError, err:
                errors.append(self._get_serialization_error(err, dc))
        self._log_serialization_errors(errors)
        RideSaveAll().publish()

    def serialize_controller(self, controller):
        try:
            self._serialize_file(controller)
        except SerializationError, err:
            self._log_serialization_errors([self._get_serialization_error(err, controller)])

    def _log_serialization_errors(self, errors):
        if errors:
            context.LOG.error('Following file(s) could not be saved:\n\n%s' %
                              '\n'.join(errors))

    def _get_serialization_error(self, err, controller):
        return '%s: %s\n' % (controller.data.source, str(err))

    def _serialize_file(self, controller):
        RideSaving(path=controller.source).publish()
        serializer = Serializer()
        serializer.serialize(controller)
        controller.unmark_dirty()
        RideSaved(path=controller.source).publish()

    def _get_all_dirty_controllers(self):
        return [controller for controller in self._get_all_controllers() if controller.dirty]

    def _get_all_controllers(self):
        return self._get_filecontroller_and_all_child_filecontrollers(self.data)\
               + self.resources

    def _get_filecontroller_and_all_child_filecontrollers(self, parent_controller):
        ret = []
        ret.append(parent_controller)
        for controller in parent_controller.children:
            ret.extend(self._get_filecontroller_and_all_child_filecontrollers(controller))
        return ret


class _DataLoader(Thread):

    def __init__(self, path):
        Thread.__init__(self)
        self._path = path
        self.datafile = None

    def run(self):
        try:
            self.datafile = TestData(source=self._path)
        except Exception, err:
            pass
            #context.LOG.error(str(err))


class _ResourceLoader(Thread):

    def __init__(self, datafile, resource_loader):
        Thread.__init__(self)
        self._datafile = datafile
        self._loader = resource_loader
        self.resources = []

    def run(self):
        self.resources = self._loader(self._datafile)
