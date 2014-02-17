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
# @author: Hareesh Puthalath, Cisco Systems, Inc.
import sys

import datetime
import mock

from neutron.openstack.common import importutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import uuidutils

sys.modules['ncclient'] = mock.MagicMock()
sys.modules['ciscoconfparse'] = mock.MagicMock()
from neutron.plugins.cisco.l3.agent.hosting_devices_manager import (
    HostingDevicesManager)
from neutron.plugins.cisco.l3.agent.router_info import RouterInfo
from neutron.tests import base

_uuid = uuidutils.generate_uuid
LOG = logging.getLogger(__name__)


class TestHostingDevice(base.BaseTestCase):

    def setUp(self):
        super(TestHostingDevice, self).setUp()
        self.hdm = HostingDevicesManager()
        self.hdm._is_pingable = mock.MagicMock()
        self.hdm._is_pingable.return_value = True

        self.hosting_device = {'id': 123,
                               'host_type': 'CSR1kv',
                               'ip_address': '10.0.0.1',
                               'port': '22',
                               'booting_time': 420}
        self.created_at_str = datetime.datetime.utcnow().strftime(
            "%Y-%m-%d %H:%M:%S")
        self.hosting_device['created_at'] = self.created_at_str
        self.router_id = _uuid()
        self.router = {id: self.router_id,
                       'hosting_device': self.hosting_device}

    def test_hosting_devices_object(self):

        self.assertEqual(self.hdm.backlog_hosting_devices, {})
        self.assertEqual(self.hdm.router_id_hosting_devices, {})
        self.assertEqual(self.hdm._drivers, {})

    def test_set_driver(self):
        ri = RouterInfo(self.router_id, self.router)
        _driver = self.hdm._set_driver(ri)

        klass = importutils.import_class('neutron.plugins.cisco.l3.'
                                         'agent.csr1000v.'
                                         'csr1000v_routing_driver.'
                                         'CSR1000vRoutingDriver')
        self.assertTrue(isinstance(_driver, klass))

    def test_is_hosting_device_reachable_positive(self):
        self.assertTrue(self.hdm.is_hosting_device_reachable(self.router_id,
                                                             self.router))

    def test_is_hosting_device_reachable_negative(self):
        self.assertEqual(len(self.hdm.backlog_hosting_devices), 0)
        self.hosting_device['created_at'] = self.created_at_str  # Back to str
        self.hdm._is_pingable.return_value = False

        self.assertFalse(self.hdm._is_pingable('1.2.3.4'))
        self.assertEqual(self.hdm.is_hosting_device_reachable(
            self.router_id, self.router), None)
        self.assertEqual(len(self.hdm.backlog_hosting_devices), 1)
        self.assertTrue(123 in self.hdm.backlog_hosting_devices.keys())
        self.assertEqual(self.hdm.backlog_hosting_devices[123]['routers'],
                         [self.router_id])

    def test_test_is_hosting_device_reachable_negative_exisiting_hd(self):
        self.hdm.backlog_hosting_devices.clear()
        self.hdm.backlog_hosting_devices[123] = {'hd': None,
                                                 'routers': [_uuid()]}

        self.assertEqual(len(self.hdm.backlog_hosting_devices), 1)
        self.assertEqual(self.hdm.is_hosting_device_reachable(
            self.router_id, self.router), None)
        self.assertEqual(len(self.hdm.backlog_hosting_devices), 1)
        self.assertTrue(123 in self.hdm.backlog_hosting_devices.keys())
        self.assertEqual(len(
            self.hdm.backlog_hosting_devices[123]['routers']), 2)

    def test_check_backlog_empty(self):

        expected = {'reachable': [],
                    'dead': []}

        self.assertEqual(self.hdm.check_backlogged_hosting_devices(),
                         expected)

    def test_check_backlog_below_booting_time(self):
        expected = {'reachable': [],
                    'dead': []}
        created_at_str_now = datetime.datetime.utcnow().strftime(
            "%Y-%m-%dT%H:%M:%S.%f")

        self.hosting_device['created_at'] = created_at_str_now
        hd = self.hosting_device
        hd_id = hd['id']
        self.hdm.backlog_hosting_devices[hd_id] = {'hd': hd,
                                                   'routers': [self.router_id]}

        self.assertEqual(self.hdm.check_backlogged_hosting_devices(),
                         expected)

        #Simulate after 100 seconds
        timedelta_100 = datetime.timedelta(seconds=100)
        created_at_100sec = datetime.datetime.utcnow() - timedelta_100
        created_at_100sec_str = created_at_100sec.strftime(
            "%Y-%m-%dT%H:%M:%S.%f")

        self.hosting_device['created_at'] = created_at_100sec_str
        self.assertEqual(self.hdm.check_backlogged_hosting_devices(),
                         expected)

        #Boundary test : 419 seconds : default 420 seconds
        timedelta_419 = datetime.timedelta(seconds=419)
        created_at_419sec = datetime.datetime.utcnow() - timedelta_419
        created_at_419sec_str = created_at_419sec.strftime(
            "%Y-%m-%dT%H:%M:%S.%f")

        self.hosting_device['created_at'] = created_at_419sec_str
        self.assertEqual(self.hdm.check_backlogged_hosting_devices(),
                         expected)

    def test_check_backlog_above_booting_time_pingable(self):
        """This test simulates a hosting device which has passed the
           created time. Device is now pingable.
        """
        #Created time : current time - 430 seconds
        timedelta_430 = datetime.timedelta(seconds=430)
        created_at_430sec = datetime.datetime.utcnow() - timedelta_430
        created_at_430sec_str = created_at_430sec.strftime(
            "%Y-%m-%dT%H:%M:%S.%f")

        self.hosting_device['created_at'] = created_at_430sec_str
        hd = self.hosting_device
        hd_id = hd['id']
        self.hdm._is_pingable.return_value = True
        self.hdm.backlog_hosting_devices[hd_id] = {'hd': hd,
                                                   'routers': [self.router_id]}
        expected = {'reachable': [hd_id],
                    'dead': []}
        self.assertEqual(self.hdm.check_backlogged_hosting_devices(),
                         expected)

    def test_check_backlog_above_BT_not_pingable_below_deadtime(self):
        """This test simulates a hosting device which has passed the created
           time but less than the 'declared dead' time.
           Hosting device is still not pingable.
        """
        #Created time : current time - 430 seconds
        timedelta_430 = datetime.timedelta(seconds=430)
        created_at_430sec = datetime.datetime.utcnow() - timedelta_430
        created_at_430sec_str = created_at_430sec.strftime(
            "%Y-%m-%dT%H:%M:%S.%f")

        hd = self.hosting_device
        hd['created_at'] = created_at_430sec_str
        #Inserted in backlog after 60 seconds
        hd['backlog_insertion_ts'] = (datetime.datetime.utcnow())
                                      # - datetime.timedelta(seconds=360))

        hd_id = hd['id']
        self.hdm._is_pingable.return_value = False
        self.hdm.backlog_hosting_devices[hd_id] = {'hd': hd,
                                                   'routers': [self.router_id]}
        expected = {'reachable': [],
                    'dead': []}
        self.assertEqual(self.hdm.check_backlogged_hosting_devices(),
                         expected)

    def test_check_backlog_above_BT_not_pingable_aboveDeadTime(self):
        """This test simulates a hosting device which has passed the
           created time but greater than the 'declared dead' time.
           Hosting device is still not pingable
        """
        #Created time: Current time - 420(Booting time) - 300(Dead time)seconds
        # Calculated as 730 adding a margin of 10 seconds
        timedelta_730 = datetime.timedelta(seconds=730)
        created_at_730sec = datetime.datetime.utcnow() - timedelta_730
        created_at_730sec_str = created_at_730sec.strftime(
            "%Y-%m-%dT%H:%M:%S.%f")

        hd = self.hosting_device
        hd['created_at'] = created_at_730sec_str
        #Inserted in backlog 5 seconds after booting time
        hd['backlog_insertion_ts'] = (datetime.datetime.utcnow() -
                                      datetime.timedelta(seconds=425))

        hd_id = hd['id']
        self.hdm._is_pingable.return_value = False
        self.hdm.backlog_hosting_devices[hd_id] = {'hd': hd,
                                                   'routers': [self.router_id]}
        expected = {'reachable': [],
                    'dead': [hd_id]}
        self.assertEqual(self.hdm.check_backlogged_hosting_devices(),
                         expected)
