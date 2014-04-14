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

import logging
import json

from neutron.plugins.cisco.cfg_agent.services_api import RoutingDriverBase

LOG = logging.getLogger(__name__)


class DummyRoutingDriver(RoutingDriverBase):
    """Dummy Routing Driver.

    """

    DEV_NAME_LEN = 14

    def __init__(self, **device_params):
        LOG.debug(json.dumps(device_params, sort_keys=True, indent=4))

    ###### Public Functions ########
    def router_added(self, ri):
        LOG.debug("DummyDriver router_added() called.")

    def router_removed(self, ri):
        LOG.debug("DummyDriver router_removed() called.")

    def internal_network_added(self, ri, port):
        LOG.debug("DummyDriver internal_network_added() called.")

    def internal_network_removed(self, ri, port):
        LOG.debug("DummyDriver internal_network_removed() called.")

    def external_gateway_added(self, ri, ex_gw_port):
        LOG.debug("DummyDriver external_gateway_added() called.")

    def external_gateway_removed(self, ri, ex_gw_port):
        LOG.debug("DummyDriver external_gateway_removed() called.")

    def enable_internal_network_NAT(self, ri, port, ex_gw_port):
        LOG.debug("DummyDriver external_gateway_added() called.")

    def disable_internal_network_NAT(self, ri, port, ex_gw_port):
        LOG.debug("DummyDriver disable_internal_network_NAT() called.")

    def floating_ip_added(self, ri, ex_gw_port, floating_ip, fixed_ip):
        LOG.debug("DummyDriver floating_ip_added() called.")

    def floating_ip_removed(self, ri, ex_gw_port, floating_ip, fixed_ip):
        LOG.debug("DummyDriver floating_ip_removed() called.")

    def routes_updated(self, ri, action, route):
        LOG.debug("DummyDriver routes_updated() called.")

    ##### Internal Functions  ####
    def clear_connection(self):
        LOG.debug("DummyDriver clear_connection() called.")


if __name__ == '__main':
    dd = DummyRoutingDriver({'name': 'DummyDriver', 'port': 22})
