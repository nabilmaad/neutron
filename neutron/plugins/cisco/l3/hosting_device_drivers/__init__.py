# Copyright 2014 Cisco Systems, Inc.  All rights reserved.
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
#
# @author: Bob Melander, Cisco Systems, Inc.

from abc import ABCMeta, abstractmethod

import six


@six.add_metaclass(ABCMeta)
class HostingDeviceDriver(object):
    """This class defines the API for hosting device drivers.

    These are used by Cisco (routing service) plugin to perform
    various (plugin independent) operations on hosting devices.
    """

    @abstractmethod
    def hosting_device_name(self):
        pass

    @abstractmethod
    def create_configdrive_files(self, context, mgmtport):
        """Creates configuration file(s) for a service VM.

        This function can be used to make initial configurations. The file(s)
        is/are injected in the VM's file system using Nova's configdrive
        feature.

        Called when a service VM-based hosting device is to be created.
        This function should cleanup after itself in case of error.

        returns: Dict with filenames and corresponding source filenames:
                 {filename1: src_filename1, filename2: src_filename2, ...}
                 The file system of the VM will contain files with the
                 specified filenames and content from the src_filename
                 files. If the dict is empty no configdrive will be used.

        :param context: neutron api request context.
        :param mgmt_port: management port for the hosting device.
        """
        pass

    @abstractmethod
    def delete_configdrive_files(self, context, mgmtport):
        """Deletes any configuration file(s) used by service VM's configdrive.

        Called when a service VM-based hosting device is to be deleted.

        returns: -

        :param context: neutron api request context.
        :param mgmt_port: management port for the hosting device.
        """
        pass
