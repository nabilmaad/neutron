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

N_ROUTER_PREFIX = 'nrouter-'


class RouterInfo(object):

    def __init__(self, router_id, router):
        self.router_id = router_id
        self.ex_gw_port = None
        self._snat_enabled = None
        self._snat_action = None
        self.internal_ports = []
        self.floating_ips = []
        self.router = router
        self.routes = []
        self.ha_info = None
        # Set 'ha_info' if present
        if router.get('ha_info') is not None:
            self.ha_info = router['ha_info']

    @property
    def router(self):
        return self._router

    @property
    def snat_enabled(self):
        return self._snat_enabled

    @router.setter
    def router(self, value):
        self._router = value
        if not self._router:
            return
        # enable_snat by default if it wasn't specified by plugin
        self._snat_enabled = self._router.get('enable_snat', True)

    def router_name(self):
        return N_ROUTER_PREFIX + self.router_id
