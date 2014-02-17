# Copyright 2013 Cisco Systems, Inc.  All rights reserved.
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

import copy
import mock
from oslo.config import cfg

from neutron.common import config as base_config
from neutron.common import constants as l3_constants
from neutron.common import exceptions as n_exc
from neutron.openstack.common import log as logging
from neutron.openstack.common import uuidutils
from neutron.plugins.cisco.l3.agent.cfg_agent import CiscoCfgAgent
from neutron.plugins.cisco.l3.agent.router_info import RouterInfo
from neutron.tests import base

from neutron.plugins.cisco.l3.common.exceptions import CSR1000vConfigException

_uuid = uuidutils.generate_uuid
HOSTNAME = 'myhost'
FAKE_ID = _uuid()

LOG = logging.getLogger(__name__)


class TestBasicRouterOperations(base.BaseTestCase):

    def setUp(self):
        super(TestBasicRouterOperations, self).setUp()
        self.conf = cfg.ConfigOpts()
        self.conf.register_opts(base_config.core_opts)
        self.conf.register_opts(CiscoCfgAgent.OPTS)
        self.ex_gw_port = {'id': _uuid(),
                           'network_id': _uuid(),
                           'fixed_ips': [{'ip_address': '19.4.4.4',
                                         'subnet_id': _uuid()}],
                           'subnet': {'cidr': '19.4.4.0/24',
                                      'gateway_ip': '19.4.4.1'}}
        self.hosting_device = {'id': _uuid(),
                               'host_type': 'CSR1000v',
                               'ip_address': '20.0.0.5',
                               'port': '23'}
        self.router = {
            'id': _uuid(),
            'enable_snat': True,
            'routes': [],
            'gw_port': self.ex_gw_port}

        #Patches & Mocks
        self.device_exists_p = mock.patch(
            'neutron.agent.linux.ip_lib.device_exists')
        self.device_exists = self.device_exists_p.start()
        self.l3pluginApi_cls_p = mock.patch(
            'neutron.plugins.cisco.l3.agent.cfg_agent.CiscoL3PluginApi')
        l3pluginApi_cls = self.l3pluginApi_cls_p.start()
        self.plugin_api = mock.Mock()
        l3pluginApi_cls.return_value = self.plugin_api
        self.looping_call_p = mock.patch(
            'neutron.openstack.common.loopingcall.FixedIntervalLoopingCall')
        self.looping_call_p.start()

        self.addCleanup(mock.patch.stopall)

    def _prepare_router_data(self, enable_snat=None, num_internal_ports=1):
        router_id = _uuid()
        ex_gw_port = {'id': _uuid(),
                      'network_id': _uuid(),
                      'fixed_ips': [{'ip_address': '19.4.4.4',
                                     'subnet_id': _uuid()}],
                      'subnet': {'cidr': '19.4.4.0/24',
                                 'gateway_ip': '19.4.4.1'}}
        int_ports = []
        for i in range(num_internal_ports):
            int_ports.append({'id': _uuid(),
                              'network_id': _uuid(),
                              'admin_state_up': True,
                              'fixed_ips': [{'ip_address': '35.4.%s.4' % i,
                                             'subnet_id': _uuid()}],
                              'mac_address': 'ca:fe:de:ad:be:ef',
                              'subnet': {'cidr': '35.4.%s.0/24' % i,
                                         'gateway_ip': '35.4.%s.1' % i}})

        router = {
            'id': router_id,
            l3_constants.INTERFACE_KEY: int_ports,
            'routes': [],
            'gw_port': ex_gw_port}
        if enable_snat is not None:
            router['enable_snat'] = enable_snat
        return router, int_ports

    def test_router_info_create(self):
        id = _uuid()
        fake_router = {}
        ri = RouterInfo(id, fake_router)

        self.assertTrue(ri.router_name().endswith(id))

    def test_router_info_create_with_router(self):
        id = _uuid()
        ri = RouterInfo(id, self.router)
        self.assertTrue(ri.router_name().endswith(id))
        self.assertEqual(ri.router, self.router)
        self.assertEqual(ri._router, self.router)
        self.assertEqual(ri.snat_enabled, True)
        self.assertEqual(ri.ex_gw_port, None)

    def test_router_info_create_snat_diabled(self):
        id = _uuid()
        self.router['enable_snat'] = False
        ri = RouterInfo(id, self.router)
        self.assertEqual(ri.snat_enabled, False)

    def test_agent_create(self):
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        self.assertTrue(isinstance(agent, CiscoCfgAgent))

    def test_process_router_throw_config_error(self):
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        router, ports = self._prepare_router_data()
        self._mock_driver_and_hosting_device(agent)
        agent.internal_network_added = mock.Mock()
        snip_name = 'CREATE_SUBINTERFACE'
        e_type = 'Fake error'
        e_tag = 'Fake error tag'
        params = {'snippet': snip_name, 'type': e_type, 'tag': e_tag}
        agent.internal_network_added.side_effect = CSR1000vConfigException(
            **params)
        ri = RouterInfo(router['id'], router=router)
        self.assertRaises(CSR1000vConfigException,
                          agent.process_router, ri)
        #Clean up updated_routers set.
        agent.updated_routers.clear()
        agent.internal_network_added.reset_mock()

    def test_process_router(self):
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        agent.process_router_floating_ips = mock.Mock()
        agent.internal_network_added = mock.Mock()
        agent.external_gateway_added = mock.Mock()
        agent.internal_network_removed = mock.Mock()
        agent.external_gateway_removed = mock.Mock()
        self._mock_driver_and_hosting_device(agent)
        router, ports = self._prepare_router_data()
        fake_floatingips1 = {'floatingips': [
            {'id': _uuid(),
             'floating_ip_address': '8.8.8.8',
             'fixed_ip_address': '7.7.7.7',
             'port_id': _uuid()}]}
        ri = RouterInfo(router['id'], router=router)
        # Process with initial values
        agent.process_router(ri)
        ex_gw_port = agent._get_ex_gw_port(ri)
        # Assert that process_floating_ips, internal_network & external network
        # added were all called with the right params
        agent.process_router_floating_ips.assert_called_with(
            ri, ex_gw_port)
        agent.process_router_floating_ips.reset_mock()
        agent.internal_network_added.assert_called_with(ri, ports[0],
                                                        ex_gw_port)
        agent.external_gateway_added.assert_called_with(ri, ex_gw_port)
        agent.internal_network_added.reset_mock()
        agent.external_gateway_added.reset_mock()
        # remap floating IP to a new fixed ip
        fake_floatingips2 = copy.deepcopy(fake_floatingips1)
        fake_floatingips2['floatingips'][0]['fixed_ip_address'] = '7.7.7.8'
        router[l3_constants.FLOATINGIP_KEY] = fake_floatingips2['floatingips']

        # Process again and check that this time only the process_floating_ips
        # was only called.
        agent.process_router(ri)
        ex_gw_port = agent._get_ex_gw_port(ri)
        agent.process_router_floating_ips.assert_called_with(
            ri, ex_gw_port)
        self.assertFalse(agent.internal_network_added.called)
        self.assertFalse(agent.external_gateway_added.called)
        agent.process_router_floating_ips.reset_mock()

        # remove just the floating ips
        del router[l3_constants.FLOATINGIP_KEY]
        # Process again and check that this time also only the
        # process_floating_ips and external_network remove was called
        agent.process_router(ri)
        ex_gw_port = agent._get_ex_gw_port(ri)
        agent.process_router_floating_ips.assert_called_with(
            ri, ex_gw_port)
        self.assertFalse(agent.internal_network_added.called)
        self.assertFalse(agent.external_gateway_added.called)
        agent.process_router_floating_ips.reset_mock()

        # now no ports so state is torn down
        del router[l3_constants.INTERFACE_KEY]
        del router['gw_port']
        # Update router_info object
        ri.router = router
        # Keep a copy of the ex_gw_port before its gone after processing.
        ex_gw_port = ri.ex_gw_port
        # Process router and verify that internal and external network removed
        # were called and floating_ips_process was called
        agent.process_router(ri)
        self.assertFalse(agent.process_router_floating_ips.called)
        self.assertFalse(agent.external_gateway_added.called)
        self.assertTrue(agent.internal_network_removed.called)
        self.assertTrue(agent.external_gateway_removed.called)
        agent.internal_network_removed.assert_called_with(ri, ports[0],
                                                          ex_gw_port)
        agent.external_gateway_removed.assert_called_with(ri, ex_gw_port)

    def test_routing_table_update(self):
        router_id = _uuid()
        router = self.router
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        agent._hdm.get_driver = mock.MagicMock()
        driver = mock.MagicMock()
        agent._hdm.get_driver.return_value = driver
        fake_route1 = {'destination': '135.207.0.0/16',
                       'nexthop': '1.2.3.4'}
        fake_route2 = {'destination': '135.207.111.111/32',
                       'nexthop': '1.2.3.4'}

        # First we set the routes to fake_route1 and see if the
        # driver.routes_updated was called with 'replace'(==add or replace)
        # and fake_route1
        router['routes'] = [fake_route1]
        ri = RouterInfo(router_id, router)
        agent.process_router(ri)

        driver.routes_updated.assert_called_with(ri, 'replace', fake_route1)

        # Now we replace fake_route1 with fake_route2. This should cause driver
        # to be invoked to delete fake_route1 and 'replace'(==add or replace)
        driver.reset_mock()
        router['routes'] = [fake_route2]
        ri.router = router
        agent.process_router(ri)

        driver.routes_updated.assert_called_with(ri, 'delete', fake_route1)
        driver.routes_updated.assert_any_call(ri, 'replace', fake_route2)

        # Now we add back fake_route1 as a new route, this should cause driver
        # to be invoked to 'replace'(==add or replace) fake_route1
        driver.reset_mock()
        router['routes'] = [fake_route2, fake_route1]
        ri.router = router
        agent.process_router(ri)

        driver.routes_updated.assert_any_call(ri, 'replace', fake_route1)

        # Now we delete all routes. This should cause driver
        # to be invoked to delete fake_route1 and fake-route2
        driver.reset_mock()
        router['routes'] = []
        ri.router = router
        agent.process_router(ri)

        driver.routes_updated.assert_any_call(ri, 'delete', fake_route2)
        driver.routes_updated.assert_any_call(ri, 'delete', fake_route1)

    def test_process_router_internal_network_added_unexpected_error(self):
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        agent._hdm.get_driver = mock.MagicMock()
        router, ports = self._prepare_router_data()
        ri = RouterInfo(router['id'], router=router)
        with mock.patch.object(
                CiscoCfgAgent,
                'internal_network_added') as internal_network_added:
            # raise RuntimeError to simulate that an unexpected exception
            # occurrs
            internal_network_added.side_effect = RuntimeError
            self.assertRaises(RuntimeError, agent.process_router, ri)
            self.assertNotIn(
                router[l3_constants.INTERFACE_KEY][0], ri.internal_ports)

            # The unexpected exception has been fixed manually
            internal_network_added.side_effect = None

            # _sync_routers_task finds out that _rpc_loop failed to process the
            # router last time, it will retry in the next run.
            agent.process_router(ri)
            # We were able to add the port to ri.internal_ports
            self.assertIn(
                router[l3_constants.INTERFACE_KEY][0], ri.internal_ports)

    def test_process_router_internal_network_removed_unexpected_error(self):
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        router, ports = self._prepare_router_data()
        ri = RouterInfo(router['id'], router=router)
        agent._hdm.get_driver = mock.MagicMock()
        # add an internal port
        agent.process_router(ri)

        with mock.patch.object(
                CiscoCfgAgent,
                'internal_network_removed') as internal_net_removed:
            # raise RuntimeError to simulate that an unexpected exception
            # occurrs
            internal_net_removed.side_effect = RuntimeError
            ri.internal_ports[0]['admin_state_up'] = False
            # The above port is set to down state, remove it.
            self.assertRaises(RuntimeError, agent.process_router, ri)
            self.assertIn(
                router[l3_constants.INTERFACE_KEY][0], ri.internal_ports)

            # The unexpected exception has been fixed manually
            internal_net_removed.side_effect = None

            # _sync_routers_task finds out that _rpc_loop failed to process the
            # router last time, it will retry in the next run.
            agent.process_router(ri)
            # We were able to remove the port from ri.internal_ports
            self.assertNotIn(
                router[l3_constants.INTERFACE_KEY][0], ri.internal_ports)

    def test_routers_with_admin_state_down(self):
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        self.plugin_api.get_external_network_id.return_value = None

        routers = [
            {'id': _uuid(),
             'admin_state_up': False,
             'external_gateway_info': {}}]
        agent._process_routers(routers)
        self.assertNotIn(routers[0]['id'], agent.router_info)

    def test_router_deleted(self):
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        agent.router_deleted(None, FAKE_ID)
        self.assertIn(FAKE_ID, agent.removed_routers)

    def test_routers_updated(self):
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        agent.routers_updated(None, [FAKE_ID])
        self.assertIn(FAKE_ID, agent.updated_routers)

    def test_removed_from_agent(self):
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        agent.router_removed_from_agent(None, {'router_id': FAKE_ID})
        self.assertIn(FAKE_ID, agent.removed_routers)

    def test_added_to_agent(self):
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        agent.router_added_to_agent(None, [FAKE_ID])
        self.assertIn(FAKE_ID, agent.updated_routers)

    def _mock_driver_and_hosting_device(self, agent):
        agent._hdm.is_hosting_device_reachable = mock.MagicMock()
        agent._hdm.is_hosting_device_reachable.return_value = True
        agent._hdm.get_driver = mock.MagicMock()

    def test_process_router_delete(self):
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        agent._hdm.get_driver = mock.MagicMock()
        ex_gw_port = {'id': _uuid(),
                      'network_id': _uuid(),
                      'fixed_ips': [{'ip_address': '19.4.4.4',
                                     'subnet_id': _uuid()}],
                      'subnet': {'cidr': '19.4.4.0/24',
                                 'gateway_ip': '19.4.4.1'}}
        router = {
            'id': _uuid(),
            'enable_snat': True,
            'routes': [],
            'gw_port': ex_gw_port,
            'hosting_device': self.hosting_device}
        agent._router_added(router['id'], router)

        hd = self.hosting_device
        #This simulates book keeping inside the _set_driver() call
        agent._hdm.router_id_hosting_devices[router['id']] = hd

        agent.router_deleted(None, router['id'])
        agent._process_router_delete()
        self.assertFalse(list(agent.removed_routers))

    def test_process_routers_with_no_ext_net_in_conf(self):
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        self._mock_driver_and_hosting_device(agent)
        self.plugin_api.get_external_network_id.return_value = 'aaa'

        routers = [
            {'id': _uuid(),
             'routes': [],
             'admin_state_up': True,
             'external_gateway_info': {'network_id': 'aaa'},
             'hosting_device': self.hosting_device}]

        agent._process_routers(routers)
        self.assertIn(routers[0]['id'], agent.router_info)

    def test_process_routers_with_no_ext_net_in_conf_and_two_net_plugin(self):
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        self._mock_driver_and_hosting_device(agent)
        routers = [
            {'id': _uuid(),
             'routes': [],
             'admin_state_up': True,
             'external_gateway_info': {'network_id': 'aaa'},
             'hosting_device': self.hosting_device}]

        agent.router_info = {}
        self.plugin_api.get_external_network_id.side_effect = (
            n_exc.TooManyExternalNetworks())
        agent._process_routers(routers)
        self.assertIn(routers[0]['id'], agent.router_info)

    def test_process_routers_with_ext_net_in_conf(self):
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        self._mock_driver_and_hosting_device(agent)
        self.plugin_api.get_external_network_id.return_value = 'aaa'

        routers = [
            {'id': _uuid(),
             'routes': [],
             'admin_state_up': True,
             'hosting_device': self.hosting_device,
             'external_gateway_info': {'network_id': 'aaa'}},
            {'id': _uuid(),
             'routes': [],
             'admin_state_up': True,
             'hosting_device': self.hosting_device,
             'external_gateway_info': {'network_id': 'bbb'}}]

        agent.router_info = {}
        self.conf.set_override('gateway_external_network_id', 'aaa')
        agent._process_routers(routers)
        self.assertIn(routers[0]['id'], agent.router_info)
        self.assertNotIn(routers[1]['id'], agent.router_info)

    def test_process_routers_with_no_bridge_no_ext_net_in_conf(self):
        agent = CiscoCfgAgent(HOSTNAME, self.conf)
        self._mock_driver_and_hosting_device(agent)
        self.plugin_api.get_external_network_id.return_value = 'aaa'

        routers = [
            {'id': _uuid(),
             'routes': [],
             'admin_state_up': True,
             'hosting_device': self.hosting_device,
             'external_gateway_info': {'network_id': 'aaa'}},
            {'id': _uuid(),
             'routes': [],
             'admin_state_up': True,
             'hosting_device': self.hosting_device,
             'external_gateway_info': {'network_id': 'bbb'}}]

        agent.router_info = {}
        self.conf.set_override('external_network_bridge', '')
        agent._process_routers(routers)
        self.assertIn(routers[0]['id'], agent.router_info)
        self.assertIn(routers[1]['id'], agent.router_info)
