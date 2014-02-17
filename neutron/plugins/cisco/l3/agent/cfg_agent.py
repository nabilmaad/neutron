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

import eventlet
import netaddr

from oslo.config import cfg

from neutron.agent.common import config
from neutron.agent.linux import external_process
from neutron.agent.linux import interface
from neutron.agent.linux import ip_lib
from neutron.agent import rpc as agent_rpc
from neutron.common import constants as l3_constants
from neutron.common import topics
from neutron.common import utils as common_utils
from neutron import context
from neutron import manager
from neutron.openstack.common import excutils
from neutron.openstack.common import lockutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import loopingcall
from neutron.openstack.common import periodic_task
from neutron.openstack.common.rpc import common as rpc_common
from neutron.openstack.common.rpc import proxy
from neutron.openstack.common import service
from neutron.plugins.cisco.l3.agent.hosting_devices_manager import (
    HostingDevicesManager)
from neutron.plugins.cisco.l3.agent.router_info import RouterInfo
from neutron.plugins.cisco.l3.common import constants as cl3_constants
from neutron.plugins.cisco.l3.common.exceptions import DriverException
from neutron import service as neutron_service

LOG = logging.getLogger(__name__)


class CiscoL3PluginApi(proxy.RpcProxy):
    """Agent side of the agent RPC API."""

    BASE_RPC_API_VERSION = '1.0'

    def __init__(self, topic, host):
        super(CiscoL3PluginApi, self).__init__(
            topic=topic, default_version=self.BASE_RPC_API_VERSION)
        self.host = host

    def get_routers(self, context, router_ids=None, hd_ids=[]):
        """Make a remote process call to retrieve the sync data for routers.

        :param context: session context
        :param router_ids: list of  routers to fetch
        :param hd_ids : hosting device ids, only routers assigned to these
                        hosting devices will be returned.
        """
        return self.call(context,
                         self.make_msg('cfg_sync_routers', host=self.host,
                                       router_ids=router_ids,
                                       hosting_device_ids=hd_ids),
                         topic=self.topic)

    def get_external_network_id(self, context):
        """Make a remote process call to retrieve the external network id.

        :param context : session context
        :raise common.RemoteError: with TooManyExternalNetworks
                                   as exc_type if there are
                                   more than one external network
        """
        return self.call(context,
                         self.make_msg('get_external_network_id',
                                       host=self.host),
                         topic=self.topic)

    def report_dead_hosting_devices(self, context, hd_ids=[]):
        """Report that a hosting device cannot be contacted (presumed dead).

        :param: context: session context
        :param: hosting_device_ids: list of non-responding hosting devices
        :return: None
        """
        # Cast since we don't expect a return value.
        self.cast(context,
                  self.make_msg('report_non_responding_hosting_devices',
                                host=self.host,
                                hosting_device_ids=hd_ids),
                  topic=self.topic)


class CiscoCfgAgent(manager.Manager):
    """Cisco Cfg Agent.

    This class defines a generic configuration agent for cisco devices which
    implement network services in the cloud backend. It is based on the
    (reference) l3-agent, and tries to preserve code where possible.

    The agent by itself does not do any configuration. All device specific
    configurations are done by hosting device drivers which implement the
    service api, (defined in service_api.py) for various services (eg: Routing)

    The main entry points in this class are the `_sync_routers_task()` and
    `_rpc_loop()` .
    """
    RPC_API_VERSION = '1.1'

    OPTS = [
        cfg.StrOpt('external_network_bridge', default='',
                   help=_("Name of bridge used for external network "
                          "traffic.")),
        cfg.StrOpt('gateway_external_network_id', default='',
                   help=_("UUID of external network for routers configured "
                          "by agents.")),
        cfg.IntOpt('sync_routers_task_interval', default=60,
                   help=_("Interval between the execution of the "
                          "sync_routers_task. Checks for full sync or "
                          "process backlogged hosting devices here. Overrides "
                          "openstack periodic scheduler default")),
        cfg.StrOpt('rpc_loop_interval', default=3,
                   help=_("Interval when the rpc loop executes. This is when "
                          "agent fetches info about the updated or removed "
                          "routers notified from a plugin side RPC.")),
    ]

    def __init__(self, host, conf=None):
        self.conf = conf or cfg.CONF
        self.router_info = {}
        self.context = context.get_admin_context_without_session()
        self.plugin_rpc = CiscoL3PluginApi(topics.L3PLUGIN, host)
        self.fullsync = True
        self.updated_routers = set()
        self.removed_routers = set()
        self.sync_progress = False
        self.admin_status_up = True
        self._hdm = HostingDevicesManager()
        self.rpc_loop = loopingcall.FixedIntervalLoopingCall(
            self._rpc_loop)
        self.rpc_loop.start(interval=self.conf.rpc_loop_interval)
        super(CiscoCfgAgent, self).__init__(host=self.conf.host)

    def _fetch_external_net_id(self):
        """Find UUID of single external network for this agent."""
        if self.conf.gateway_external_network_id:
            return self.conf.gateway_external_network_id
        # Cfg agent doesn't use external_network_bridge to handle external
        # networks, so bridge_mappings with provider networks will be used
        # and the cfg agent is able to handle any external networks.
        if not self.conf.external_network_bridge:
            return
        try:
            return self.plugin_rpc.get_external_network_id(self.context)
        except rpc_common.RemoteError as e:
            with excutils.save_and_reraise_exception():
                if e.exc_type == 'TooManyExternalNetworks':
                    msg = _(
                        "The 'gateway_external_network_id' option must be "
                        "configured for this agent as Neutron has more than "
                        "one external network.")
                    raise Exception(msg)

    def _router_added(self, router_id, router):
        """Operations when a router is added.

        Create a new RouterInfo object for this router and add it to the
        agent's router_info dictionary.  Then `router_added()` is called on
        the driver.

        :param router_id: id of the router
        :param router: router dict
        :return: None
        """
        ri = RouterInfo(router_id, router)
        driver = self._hdm.get_driver(ri)
        driver.router_added(ri)
        self.router_info[router_id] = ri

    def _router_removed(self, router_id, deconfigure=True):
        """Operations when a router is removed.

        Get the RouterInfo object corresponding to the router in the agent's
        router_info dict. If deconfigure is set to True, remove the router's
        configuration from the hosting device.
        :param router_id: id of the router
        :param deconfigure: if True, the router's configuration is deleted from
        the hosting device.
        :return:
        """
        ri = self.router_info.get(router_id)
        if ri is None:
            LOG.warn(_("Info for router %s was not found. "
                       "Skipping router removal"), router_id)
            return
        ri.router['gw_port'] = None
        ri.router[l3_constants.INTERFACE_KEY] = []
        ri.router[l3_constants.FLOATINGIP_KEY] = []
        try:
            if deconfigure:
                self.process_router(ri)
                driver = self._hdm.get_driver(ri)
                driver.router_removed(ri, deconfigure)
                self._hdm.remove_driver(router_id)
            del self.router_info[router_id]
        except DriverException:
            LOG.info(_("Router remove for router_id: %s was incomplete. "
                       "Adding the router to removed_routers list"), router_id)
            self.removed_routers.add(router_id)
            # remove this router from updated_routers if it is there. It might
            # end up there too if exception was thrown inside `process_router`
            self.updated_routers.discard(router_id)

    def _set_subnet_info(self, port):
        ips = port['fixed_ips']
        if not ips:
            raise Exception(_("Router port %s has no IP address") % port['id'])
        if len(ips) > 1:
            LOG.error(_("Ignoring multiple IPs on router port %s"),
                      port['id'])
        prefixlen = netaddr.IPNetwork(port['subnet']['cidr']).prefixlen
        port['ip_cidr'] = "%s/%s" % (ips[0]['ip_address'], prefixlen)

    def _get_ex_gw_port(self, ri):
        return ri.router.get('gw_port')

    def process_router(self, ri):
        """Process a router, apply latest configuration and update router_info.

        Get the router dict from  RouterInfo and proceed to detect changes
        from the last known state. When new ports or deleted ports are
        detected, `internal_network_added()` or `internal_networks_removed()`
        are called accordingly. Similarly changes in ex_gw_port causes
         `external_gateway_added()` or `external_gateway_removed()` calls.
        Next, floating_ips and routes are processed. Also, latest state is
        stored in ri.internal_ports and ri.ex_gw_port for future comparisons.

        :param ri : neutron.plugins.cisco.l3.agent.router_info.RouterInfo
        corresponding to the router being processed.
        :return:None
        :raises: neutron.plugins.cisco.l3.common.exceptions.DriverException if
        the configuration operation fails.
        """
        try:
            ex_gw_port = self._get_ex_gw_port(ri)
            ri.ha_info = ri.router.get('ha_info', None)
            internal_ports = ri.router.get(l3_constants.INTERFACE_KEY, [])
            existing_port_ids = set([p['id'] for p in ri.internal_ports])
            current_port_ids = set([p['id'] for p in internal_ports
                                    if p['admin_state_up']])
            new_ports = [p for p in internal_ports
                         if p['id'] in (current_port_ids - existing_port_ids)]
            old_ports = [p for p in ri.internal_ports
                         if p['id'] not in current_port_ids]

            for p in new_ports:
                self._set_subnet_info(p)
                self.internal_network_added(ri, p, ex_gw_port)
                ri.internal_ports.append(p)

            for p in old_ports:
                self.internal_network_removed(ri, p, ri.ex_gw_port)
                ri.internal_ports.remove(p)

            if ex_gw_port and not ri.ex_gw_port:
                self._set_subnet_info(ex_gw_port)
                self.external_gateway_added(ri, ex_gw_port)
            elif not ex_gw_port and ri.ex_gw_port:
                self.external_gateway_removed(ri, ri.ex_gw_port)

            if ex_gw_port:
                self.process_router_floating_ips(ri, ex_gw_port)

            ri.ex_gw_port = ex_gw_port
            self.routes_updated(ri)
        except DriverException as e:
            with excutils.save_and_reraise_exception():
                LOG.error(e)

    def process_router_floating_ips(self, ri, ex_gw_port):
        """Process a router's floating ips.

        Compare current floatingips (in ri.floating_ips) with the router's
        updated floating ips (in ri.router.floating_ips) and detect
        flaoting_ips which were added or removed. Notify driver of
        the change via `floating_ip_added()` or `floating_ip_removed()`.

        :param ri:  neutron.plugins.cisco.l3.agent.router_info.RouterInfo
        corresponding to the router being processed.
        :param ex_gw_port: Port dict of the external gateway port.
        :return: None
        :raises: neutron.plugins.cisco.l3.common.exceptions.DriverException if
        the configuration operation fails.
        """

        floating_ips = ri.router.get(l3_constants.FLOATINGIP_KEY, [])
        existing_floating_ip_ids = set(
            [fip['id'] for fip in ri.floating_ips])
        cur_floating_ip_ids = set([fip['id'] for fip in floating_ips])

        id_to_fip_map = {}

        for fip in floating_ips:
            if fip['port_id']:
                # store to see if floatingip was remapped
                id_to_fip_map[fip['id']] = fip
                if fip['id'] not in existing_floating_ip_ids:
                    ri.floating_ips.append(fip)
                    self.floating_ip_added(ri, ex_gw_port,
                                           fip['floating_ip_address'],
                                           fip['fixed_ip_address'])

        floating_ip_ids_to_remove = (existing_floating_ip_ids -
                                     cur_floating_ip_ids)
        for fip in ri.floating_ips:
            if fip['id'] in floating_ip_ids_to_remove:
                ri.floating_ips.remove(fip)
                self.floating_ip_removed(ri, ri.ex_gw_port,
                                         fip['floating_ip_address'],
                                         fip['fixed_ip_address'])
            else:
                # handle remapping of a floating IP
                new_fip = id_to_fip_map[fip['id']]
                new_fixed_ip = new_fip['fixed_ip_address']
                existing_fixed_ip = fip['fixed_ip_address']
                if (new_fixed_ip and existing_fixed_ip and
                        new_fixed_ip != existing_fixed_ip):
                    floating_ip = fip['floating_ip_address']
                    self.floating_ip_removed(ri, ri.ex_gw_port,
                                             floating_ip,
                                             existing_fixed_ip)
                    self.floating_ip_added(ri, ri.ex_gw_port,
                                           floating_ip, new_fixed_ip)
                    ri.floating_ips.remove(fip)
                    ri.floating_ips.append(new_fip)

    def external_gateway_added(self, ri, ex_gw_port):
        driver = self._hdm.get_driver(ri)
        driver.external_gateway_added(ri, ex_gw_port)
        if ri.snat_enabled and ri.internal_ports:
            for port in ri.internal_ports:
                driver.enable_internal_network_NAT(ri, port, ex_gw_port)

    def external_gateway_removed(self, ri, ex_gw_port):
        driver = self._hdm.get_driver(ri)
        if ri.snat_enabled and ri.internal_ports:
            for port in ri.internal_ports:
                driver.disable_internal_network_NAT(ri, port, ex_gw_port)
        driver.external_gateway_removed(ri, ex_gw_port)

    def internal_network_added(self, ri, port, ex_gw_port):
        driver = self._hdm.get_driver(ri)
        driver.internal_network_added(ri, port)
        if ri.snat_enabled and ex_gw_port:
            driver.enable_internal_network_NAT(ri, port, ex_gw_port)

    def internal_network_removed(self, ri, port, ex_gw_port):
        driver = self._hdm.get_driver(ri)
        driver.internal_network_removed(ri, port)
        if ri.snat_enabled and ex_gw_port:
            driver.disable_internal_network_NAT(ri, port, ex_gw_port)

    def floating_ip_added(self, ri, ex_gw_port, floating_ip, fixed_ip):
        driver = self._hdm.get_driver(ri)
        driver.floating_ip_added(ri, ex_gw_port, floating_ip, fixed_ip)

    def floating_ip_removed(self, ri, ex_gw_port, floating_ip, fixed_ip):
        driver = self._hdm.get_driver(ri)
        driver.floating_ip_removed(ri, ex_gw_port, floating_ip, fixed_ip)

    def router_deleted(self, context, router_id):
        """Deal with router deletion RPC message."""
        LOG.debug(_('Got router deleted notification for %s'), router_id)
        self.removed_routers.add(router_id)

    def routers_updated(self, context, routers):
        """Deal with routers modification and creation RPC message."""
        LOG.debug(_('Got routers updated notification :%s'), routers)
        if routers:
            # This is needed for backward compatibility
            if isinstance(routers[0], dict):
                routers = [router['id'] for router in routers]
            self.updated_routers.update(routers)

    def hosting_device_removed(self, context, payload):
        """Deal with hosting device removed RPC message.
        Payload format:
        {
             'hosting_data': {'hd_id1': {'routers': [id1, id2, ...]},
                              'hd_id2': {'routers': [id3, id4, ...]}, ... },
             'deconfigure': True/False
        }
        """
        for hd_id, resource_data in payload['hosting_data'].items():
            LOG.debug(_("Hosting device removal data: %s "),
                      payload['hosting_data'])
            for router_id in resource_data.get('routers', []):
                self._router_removed(router_id, payload['deconfigure'])
            self._hdm.pop(hd_id)

    def router_removed_from_agent(self, context, payload):
        LOG.debug(_('Got router removed from agent :%r'), payload)
        self.removed_routers.add(payload['router_id'])

    def router_added_to_agent(self, context, payload):
        LOG.debug(_('Got router added to agent :%r'), payload)
        self.routers_updated(context, payload)

    def _process_routers(self, routers, all_routers=False):
        """Process the set of routers.

        Iterating on the set of routers received and comparing it with the
        set of routers already in the agent, new routers which are added are
        identified. Then check the reachability (via ping) of hosting device
        where the router is hosted and backlogs it if necessary. For routers
        which are only updated, call `process_router()` on them. Note that
        for each router this is done in an independent thread.

        When all_routers is set to True, this will result in the detection and
        deletion of routers which are removed also.
        :param routers: The set of routers to be processed
        :param all_routers: Flag for specifying a partial list of routers
        :return: None
        """
        pool = eventlet.GreenPool()
        if (self.conf.external_network_bridge and
                not (ip_lib.device_exists(self.conf.external_network_bridge))):
            LOG.error(_("The external network bridge '%s' does not exist"),
                      self.conf.external_network_bridge)
            return

        target_ex_net_id = self._fetch_external_net_id()
        # if routers are all the routers we have (they are from router sync on
        # starting or when error occurs during running), we seek the
        # routers which should be removed.
        # If routers are from server side notification, we seek them
        # from subset of incoming routers and ones we have now.
        if all_routers:
            prev_router_ids = set(self.router_info)
        else:
            prev_router_ids = set(self.router_info) & set(
                [router['id'] for router in routers])
        cur_router_ids = set()
        for r in routers:
            if not r['admin_state_up']:
                continue
            # Note: Whether the router can only be assigned to a particular
            # hosting device is decided and enforced by the plugin.
            # So no checks are done here.
            ex_net_id = (r['external_gateway_info'] or {}).get('network_id')
            if (target_ex_net_id and ex_net_id and
                    ex_net_id != target_ex_net_id):
                continue
            cur_router_ids.add(r['id'])
            if not self._hdm.is_hosting_device_reachable(r['id'], r):
                LOG.info(_("Router: %(id)s is on unreachable hosting device. "
                           "Skip processing it."), {'id': r['id']})
                continue
            if r['id'] not in self.router_info:
                self._router_added(r['id'], r)
            ri = self.router_info[r['id']]
            ri.router = r
            pool.spawn_n(self.process_router, ri)
        # identify and remove routers that no longer exist
        for router_id in prev_router_ids - cur_router_ids:
            pool.spawn_n(self._router_removed, router_id)
        pool.waitall()

    @lockutils.synchronized('cisco-cfg-agent', 'neutron-')
    def _rpc_loop(self):
        """Process routers received via RPC.

        This method  executes every `RPC_LOOP_INTERVAL` seconds and processes
        routers which have been notified via RPC from the plugin. Plugin sends
        RPC messages for updated or removed routers, whose router_ids are kept
        in `updated_routers` and `removed_routers` respectively. For router in
        `updated_routers` we fetch the latest state for these routers from
        the plugin and process them. Routers in `removed_routers` are
        removed from the hosting device and from the set of routers which the
        agent is tracking (router_info attribute).

        Note that this will not be executed at the same time as the
        `_sync_routers_task()` because of the lock which avoids race conditions
         on `updated_routers` and `removed_routers`

        :return: None
        """
        try:
            LOG.debug(_("Starting RPC loop for %d updated routers"),
                      len(self.updated_routers))
            if self.updated_routers:
                router_ids = list(self.updated_routers)
                self.updated_routers.clear()
                routers = self.plugin_rpc.get_routers(
                    self.context, router_ids)
                self._process_routers(routers)
            if self.removed_routers:
                self._process_router_delete()
            LOG.debug(_("RPC loop successfully completed"))
        except Exception:
            LOG.exception(_("Failed synchronizing routers"))
            self.fullsync = True

    def _process_router_delete(self):
        """Process routers in the `removed_routers` set."""
        current_removed_routers = list(self.removed_routers)
        for router_id in current_removed_routers:
            self._router_removed(router_id)
            self.removed_routers.remove(router_id)

    def _process_backlogged_hosting_devices(self, context):
        """Process currently back logged devices.

        Go through the currently backlogged devices and process them.
        For devices which are now reachable (compared to last time), we fetch
        the routers they are hosting and process them.
        For devices which have passed the `hosting_device_dead_timeout` and
        hence presumed dead, execute a RPC to the plugin informing that.
        :param context: RPC context
        :return: None
        """
        res = self._hdm.check_backlogged_hosting_devices()
        if res['reachable']:
            #Fetch routers for this reachable Hosting Device
            LOG.debug(_("Requesting routers for hosting devices: %s "
                        "that are now responding."), res['reachable'])
            routers = self.plugin_rpc.get_routers(
                context, router_ids=None, hd_ids=res['reachable'])
            self._process_routers(routers, all_routers=True)
        if res['dead']:
            LOG.debug(_("Reporting dead hosting devices: %s"),
                      res['dead'])
            # Process dead hosting device
            self.plugin_rpc.report_dead_hosting_devices(
                context, hd_ids=res['dead'])

    @periodic_task.periodic_task
    @lockutils.synchronized('cisco-cfg-agent', 'neutron-')
    def _sync_routers_task(self, context):
        """Synchronize routers on this agent.

        Synchronizes routers with period of `sync_routers_task_interval`
        When this method executes and `full_sync` flag is True, this method
        fetches all the routers from the plugin and process them. This can
        cause routers to be added/modified/deleted from the hosting device.
        When `full_sync` is False, we process all the backlogged hosting
        devices.
        :param context: RPC_context
        :return: None
        """
        LOG.debug(_("Starting _sync_routers_task - fullsync:%s"),
                  self.fullsync)
        if self.fullsync:
            try:
                LOG.debug(_("Starting a full sync"))
                router_ids = None
                self.updated_routers.clear()
                self.removed_routers.clear()
                routers = self.plugin_rpc.get_routers(
                    context, router_ids)
                LOG.debug(_('Processing :%r'), routers)
                self._process_routers(routers, all_routers=True)
                self.fullsync = False
                LOG.debug(_("_sync_routers_task successfully completed"))
            except Exception:
                LOG.exception(_("Failed synchronizing routers"))
                self.fullsync = True
        else:
            LOG.debug(_("Full sync is False. Processing backlog."))
            self._process_backlogged_hosting_devices(context)

    def after_start(self):
        LOG.info(_("Cisco cfg agent started"))

    def routes_updated(self, ri):
        """Update the state of routes in the router.

         Compares the current routes with the (configured) existing routes
         and detect what was removed or added. Then configure the
         logical router in the hosting device accordingly.
        :param ri: router_info corresponding to the router.
        :return: None
        :raises: neutron.plugins.cisco.l3.common.exceptions.DriverException if
        the configuration operation fails.
        """
        new_routes = ri.router['routes']
        old_routes = ri.routes
        adds, removes = common_utils.diff_list_of_dict(old_routes,
                                                       new_routes)
        for route in adds:
            LOG.debug(_("Added route entry is '%s'"), route)
            # remove replaced route from deleted route
            for del_route in removes:
                if route['destination'] == del_route['destination']:
                    removes.remove(del_route)
            #replace success even if there is no existing route
            driver = self._hdm.get_driver(ri)
            driver.routes_updated(ri, 'replace', route)

        for route in removes:
            LOG.debug(_("Removed route entry is '%s'"), route)
            driver = self._hdm.get_driver(ri)
            driver.routes_updated(ri, 'delete', route)
        ri.routes = new_routes


class CiscoCfgAgentWithStateReport(CiscoCfgAgent):

    def __init__(self, host, conf=None):
        super(CiscoCfgAgentWithStateReport, self).__init__(host=host,
                                                           conf=conf)
        self.state_rpc = agent_rpc.PluginReportStateAPI(topics.PLUGIN)
        self.agent_state = {
            'binary': 'neutron-cisco-cfg-agent',
            'host': host,
            'topic': cl3_constants.CFG_AGENT,
            'configurations': {
                'hosting_device_drivers': {
                    cl3_constants.CSR1KV_HOST:
                    'neutron.plugins.cisco.l3.agent.csr1000v.'
                    'cisco_csr_network_driver.CSR1000vRoutingDriver'}},
            'start_flag': True,
            'agent_type': cl3_constants.AGENT_TYPE_CFG}
        report_interval = cfg.CONF.AGENT.report_interval
        self.use_call = True
        if report_interval:
            self.heartbeat = loopingcall.FixedIntervalLoopingCall(
                self._report_state)
            self.heartbeat.start(interval=report_interval)

    def _report_state(self):
        """Report state back to the plugin.

        This is run every `report_interval` period. This attribute is part
        of the agent's configuration.
        Collects, creates and sends a RPC notification with a summary of
        logical routers, hosting devices and other attributes which together
        represent a snapshot of what this agent is managing.
        Look at the `configurations` dict for the parameters reported.
        :return: None
        """
        LOG.debug(_("Report state task started"))
        num_ex_gw_ports = 0
        num_interfaces = 0
        num_floating_ips = 0
        router_infos = self.router_info.values()
        num_routers = len(router_infos)
        num_hd_routers = {}
        routers_per_hd = {}
        for ri in router_infos:
            ex_gw_port = self._get_ex_gw_port(ri)
            if ex_gw_port:
                num_ex_gw_ports += 1
            num_interfaces += len(ri.router.get(
                l3_constants.INTERFACE_KEY, []))
            num_floating_ips += len(ri.router.get(
                l3_constants.FLOATINGIP_KEY, []))
            hd = ri.router['hosting_device']
            if hd:
                num_hd_routers[hd['id']] = num_hd_routers.get(hd['id'], 0) + 1
        for (hd_id, num) in num_hd_routers.items():
            routers_per_hd[hd_id] = {'routers': num}
        non_responding = self._hdm.get_backlogged_hosting_devices()
        configurations = self.agent_state['configurations']
        configurations['total routers'] = num_routers
        configurations['total ex_gw_ports'] = num_ex_gw_ports
        configurations['total interfaces'] = num_interfaces
        configurations['total floating_ips'] = num_floating_ips
        configurations['hosting_devices'] = routers_per_hd
        configurations['non_responding_hosting_devices'] = non_responding
        try:
            self.state_rpc.report_state(self.context, self.agent_state,
                                        self.use_call)
            self.agent_state.pop('start_flag', None)
            self.use_call = False
            LOG.debug(_("Report state task successfully completed"))
        except AttributeError:
            # This means the server does not support report_state
            LOG.warn(_("Neutron server does not support state report."
                       " State report for this agent will be disabled."))
            self.heartbeat.stop()
            return
        except Exception:
            LOG.exception(_("Failed reporting state!"))

    def agent_updated(self, context, payload):
        """Handle the agent_updated notification event.

        Plugin sets the `admin_status_up` flag. If `admin-status-up` is set,
        we set full_sync, which will cause a full refresh for routers
        belonging to this agent.
        Payload format : {'admin_state_up': admin_state_up}
        """
        LOG.debug(_("Agent_updated by plugin.Payload is  %s!"), payload)
        admin_status_up = payload.get('admin_state_up', None)
        if admin_status_up is not None:
            if admin_status_up and not self.admin_status_up:
                LOG.info(_("Admin status up is now True. Setting full sync"))
                self.fullsync = True
            self.admin_status_up = admin_status_up


def main(manager='neutron.plugins.cisco.l3.agent.'
                 'cfg_agent.CiscoCfgAgentWithStateReport'):
    eventlet.monkey_patch()
    conf = cfg.CONF
    conf.register_opts(CiscoCfgAgent.OPTS)
    config.register_agent_state_opts_helper(conf)
    config.register_root_helper(conf)
    conf.register_opts(interface.OPTS)
    conf.register_opts(external_process.OPTS)
    conf(project='neutron')
    config.setup_logging(conf)
    server = neutron_service.Service.create(
        binary='neutron-cisco-cfg-agent',
        topic=cl3_constants.CFG_AGENT,
        report_interval=cfg.CONF.AGENT.report_interval,
        manager=manager)
    service.launch(server).wait()
