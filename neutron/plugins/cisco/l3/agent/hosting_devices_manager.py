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

import datetime

from oslo.config import cfg

from neutron.agent.linux import utils as linux_utils
from neutron.openstack.common import importutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import timeutils

from neutron.plugins.cisco.l3.agent.router_info import RouterInfo
from neutron.plugins.cisco.l3.common import constants as cl3_constants

LOG = logging.getLogger(__name__)

OPTS = [
    cfg.IntOpt('device_connection_timeout', default=30,
               help=_("Timeout value for connecting to a hosting device")),
    cfg.IntOpt('hosting_device_dead_timeout', default=300,
               help=_("The time in seconds until a backlogged hosting device "
                      "is presumed dead. This value should be set up high "
                      "enough to recover from a period of connectivity loss "
                      "or high load when the device may not be responding.")),
    cfg.StrOpt('CSR1kv_Routing_Driver', default='neutron.plugins.cisco.'
                                                'l3.agent.csr1000v.'
                                                'csr1000v_routing_driver.'
                                                'CSR1000vRoutingDriver',
               help=_("CSR1000v Routing Driver class")),
]
cfg.CONF.register_opts(OPTS)


class HostingDevicesManager(object):
    """This class acts as a manager for different hosting devices.

    The hosting devices manager  keeps the relationship between the
    different logical resources (eg: routers) and where they are
    hosted. For configuring these logical resources in a hosting device, a set
    of driver objects are used. The driver objects are device and service
    specific. When a get_driver() call is made for a specific resource, the
    hosting device for that resource is extracted from the resource dicts
    'hosting_device' key. If a driver for that particular hosting device and
    service combo is found, it is reused, else a new driver is instantiated
    and returned.

    New drivers can be specified by adding the corresponding class to the OPTS
    variable and setting a (hosting_device_type, service_type) tuple in the
    host_driver_binding attribute which is searched for instantiating a
    driver class.
    """

    def __init__(self):
        self.router_id_hosting_devices = {}
        self._drivers = {}
        self.backlog_hosting_devices = {}
        self.host_driver_binding = {
            (cl3_constants.CSR_ROUTER_TYPE, cl3_constants.SERVICE_ROUTING,
             cl3_constants.DEV_CFG_PROTO_NETCONF):
            cfg.CONF.CSR1kv_Routing_Driver,
        }

    def get_driver(self, router_info):
        if isinstance(router_info, RouterInfo):
            router_id = router_info.router_id
        else:
            raise TypeError("Expected RouterInfo object. "
                            "Got %s instead"), type(router_info)
        hosting_device = self.router_id_hosting_devices.get(router_id, None)
        if hosting_device is not None:
            driver = self._drivers.get(hosting_device['id'], None)
            if driver is None:
                driver = self._set_driver(router_info)
        else:
            driver = self._set_driver(router_info)
        return driver

    def _set_driver(self, router_info):
        try:
            _driver = None
            router_id = router_info.router_id
            router = router_info.router

            hosting_device = router['hosting_device']
            _hd_id = hosting_device['id']
            _hd_type = hosting_device['host_type']
            # Note that we are setting  service as 'Routing' and configuration
            # protocol as 'NETCONF' as the defaults if they are not specified.
            _service_type = hosting_device.get('service_type',
                                               cl3_constants.SERVICE_ROUTING)
            _config_protocol = hosting_device.get(
                'config_protocol', cl3_constants.DEV_CFG_PROTO_NETCONF)

            # Lookup driver based on hd_type, service and config protocol
            try:
                driver_class = self.host_driver_binding[
                    (_hd_type, _service_type, _config_protocol)]
            except KeyError:
                LOG.exception(_("Cannot find driver class for "
                                "device type:%(device_type)s, service_type:"
                                "%(service_type)s and config_protocol:"
                                "%(config_protocol)s"),
                              {'device_type': _hd_type,
                               'service_type': _service_type,
                               'config_protocol': _config_protocol})
                raise
            #Load the driver
            try:
                _driver = importutils.import_object(
                    driver_class,
                    **hosting_device)
            except ImportError:
                LOG.exception(_("Error loading hosting device driver "
                                "%(driver)s for host type %(host_type)s"),
                              {'driver': driver_class,
                               'host_type': _hd_type})
                raise
            self.router_id_hosting_devices[router_id] = hosting_device
            self._drivers[_hd_id] = _driver
        except (AttributeError, KeyError) as e:
            LOG.error(_("Cannot set driver for router. Reason: %s"), e)
        return _driver

    def clear_driver_connection(self, hd_id):
            driver = self._drivers.get(hd_id, None)
            if driver:
                driver.clear_connection()
                LOG.debug(_("Cleared connection @ %s"), driver._csr_host)

    def remove_driver(self, router_id):
        del self.router_id_hosting_devices[router_id]
        for hd_id in self._drivers.keys():
            if hd_id not in self.router_id_hosting_devices.values():
                del self._drivers[hd_id]

    def pop(self, hd_id):
        self._drivers.pop(hd_id, None)

    def get_backlogged_hosting_devices(self):
        backlogged_hosting_devices = {}
        for (hd_id, data) in self.backlog_hosting_devices.items():
            backlogged_hosting_devices[hd_id] = {
                'affected routers': data['routers']}
        return backlogged_hosting_devices

    def is_hosting_device_reachable(self, router_id, router):
        hd = router['hosting_device']
        hd_id = hd['id']
        hd_mgmt_ip = hd['ip_address']
        #Modifying the 'created_at' to a date time object
        hd['created_at'] = datetime.datetime.strptime(hd['created_at'],
                                                      '%Y-%m-%d %H:%M:%S')

        if hd_id not in self.backlog_hosting_devices.keys():
            if self._is_pingable(hd_mgmt_ip):
                LOG.debug(_("Hosting device: %(hd_id)s @ %(ip)s for router: "
                            "%(id)s is reachable."),
                          {'hd_id': hd_id, 'ip': hd['ip_address'],
                           'id': router_id})
                return True
            LOG.debug(_("Hosting device: %(hd_id)s @ %(ip)s for router: "
                        "%(id)s is NOT reachable."),
                      {'hd_id': hd_id, 'ip': hd['ip_address'],
                       'id': router_id, })
            hd['backlog_insertion_ts'] = max(
                timeutils.utcnow(),
                hd['created_at'] +
                datetime.timedelta(seconds=hd['booting_time']))
            self.backlog_hosting_devices[hd_id] = {'hd': hd,
                                                   'routers': [router_id]}
            self.clear_driver_connection(hd_id)
            LOG.debug(_("Hosting device: %(hd_id)s @ %(ip)s is now added "
                        "to backlog"), {'hd_id': hd_id,
                                        'ip': hd['ip_address']})
        else:
            self.backlog_hosting_devices[hd_id]['routers'].append(router_id)

    def check_backlogged_hosting_devices(self):
        """"Checks the status of backlogged hosting devices.

        Has the intelligence to give allowance for the booting time for
        newly spun up instances. Sends back a response dict of the format:
        {'reachable': [<hd_id>,..], 'dead': [<hd_id>,..]}
        """
        response_dict = {'reachable': [],
                         'dead': []}
        for hd_id in self.backlog_hosting_devices.keys():
            hd = self.backlog_hosting_devices[hd_id]['hd']
            if not timeutils.is_older_than(hd['created_at'],
                                           hd['booting_time']):
                LOG.info(_("Hosting device: %(hd_id)s @ %(ip)s hasn't passed "
                           "minimum boot time. Skipping it. "),
                         {'hd_id': hd_id, 'ip': hd['ip_address']})
                continue
            LOG.info(_("Checking hosting device: %(hd_id)s @ %(ip)s for "
                       "reachability."), {'hd_id': hd_id,
                                          'ip': hd['ip_address']})
            if self._is_pingable(hd['ip_address']):
                hd.pop('backlog_insertion_ts', None)
                del self.backlog_hosting_devices[hd_id]
                response_dict['reachable'].append(hd_id)
                LOG.info(_("Hosting device: %(hd_id)s @ %(ip)s is now "
                           "reachable. Adding it to response"),
                         {'hd_id': hd_id, 'ip': hd['ip_address']})
            else:
                LOG.info(_("Hosting device: %(hd_id)s @ %(ip)s still not "
                           "reachable "), {'hd_id': hd_id,
                                           'ip': hd['ip_address']})
                if timeutils.is_older_than(
                        hd['backlog_insertion_ts'],
                        cfg.CONF.hosting_device_dead_timeout):
                    LOG.debug(_("Hosting device: %(hd_id)s @ %(ip)s hasn't "
                                "been reachable for the last %(time)d "
                                "seconds. Marking it dead."),
                              {'hd_id': hd_id, 'ip': hd['ip_address'],
                               'time': cfg.CONF.hosting_device_dead_timeout})
                    response_dict['dead'].append(hd_id)
                    hd.pop('backlog_insertion_ts', None)
                    del self.backlog_hosting_devices[hd_id]
        LOG.debug(_("Response: %s"), response_dict)
        return response_dict

    def _is_pingable(self, mgmt_ip):
        r = self._send_ping(mgmt_ip)
        if r:
            return False
        return True

    def _send_ping(self, ip):
        ping_cmd = ['ping',
                    '-c', '5',
                    '-W', '1',
                    '-i', '0.2',
                    ip]
        try:
            linux_utils.execute(ping_cmd, check_exit_code=True)
        except RuntimeError:
            LOG.warn(_("Cannot ping ip address: %s"), ip)
            return -1
