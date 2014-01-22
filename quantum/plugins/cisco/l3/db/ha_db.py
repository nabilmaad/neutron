# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
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
# @author: Bob Melander, Cisco Systems, Inc.

import copy
import random

from oslo.config import cfg
import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.orm import exc
from sqlalchemy.orm import joinedload

from quantum.api.v2 import attributes
from quantum.common import constants as l3_constants
from quantum.common import exceptions as q_exc
from quantum import context
from quantum.db import l3_db
from quantum.db import model_base
from quantum.db import models_v2
from quantum.extensions import l3
from quantum import manager
from quantum.openstack.common import log as logging
from quantum.openstack.common import uuidutils
from quantum.plugins.cisco.l3.extensions import ha
from quantum.plugins.cisco.l3.common import constants as cl3_const
from quantum.plugins.cisco.l3.extensions import ha


LOG = logging.getLogger(__name__)

MAX_VRRP_GROUPS = 4094
MAX_HSRP_GROUPS = 4094
MAX_GLBP_GROUPS = 1023

DEVICE_OWNER_ROUTER_GW = l3_constants.DEVICE_OWNER_ROUTER_GW
DEVICE_OWNER_ROUTER_INTF = l3_constants.DEVICE_OWNER_ROUTER_INTF
DEFAULT_MASTER_PRIORITY = 10
PRIORITY_INCREASE_STEP = 10
REDUNDANCY_ROUTER_SUFFIX = '_HA_backup_'
DEFAULT_PING_INTERVAL = 2
PING_TARGET_OPT_NAME = 'default_ping_target'

router_appliance_opts = [
    cfg.BoolOpt('ha_support_enabled', default=True,
                help=_("Enables high-availability support")),
    cfg.BoolOpt('ha_enabled_by_default', default=False,
                help=_("Enables high-availability functionality for Neutron "
                       "router even if user does not explicitly request it")),
    cfg.IntOpt('default_ha_redundancy_level', default=ha.MIN_REDUNDANCY_LEVEL,
               help=_("Default number of routers added for redundancy when "
                      "high-availability by VRRP, HSRP, or GLBP is used")),
    cfg.StrOpt('default_ha_mechanism', default=ha.HA_HSRP,
               help=_("Default mechanism used to implement "
                      "high-availability")),
    cfg.ListOpt('disabled_ha_mechanisms', default=[],
                help=_("List of administratively disabled high-availability "
                       "mechanisms (VRRP, HSRP, GBLP)")),
    cfg.BoolOpt('connectivity_probing_enabled_by_default', default=False,
                help=_("Enables connectivity probing for high-availability "
                       "even if user does not explicitly request it")),
    cfg.StrOpt(PING_TARGET_OPT_NAME, default=None,
               help=_("Host that will be ping target for high-availability "
                      "connectivity probing if user does not specify it")),
    cfg.StrOpt('default_ping_interval', default=DEFAULT_PING_INTERVAL,
               help=_("Time (in seconds) between pings for high-availability "
                      "connectivity probing if user does not specify it")),
    ]

cfg.CONF.register_opts(router_appliance_opts)


class RouterHASetting(model_base.BASEV2):
    """Represents HA settings for router visible to user."""
    router_id = sa.Column(sa.String(36),
                          sa.ForeignKey('routers.id', ondelete='CASCADE'),
                          primary_key=True)
    # 'ha_type' can be 'VRRP', 'HSRP', or 'GLBP'
    ha_type = sa.Column(sa.String(255))
    # 'redundancy_level' is number of extra routers for redundancy
    redundancy_level = sa.Column(sa.Integer, default=ha.MIN_REDUNDANCY_LEVEL)
    # 'priority' is the priority used in VRRP, HSRP, and GLBP
    priority = sa.Column(sa.Integer)
    # 'probe_connectivity' is True if ICMP echo pinging is enabled
    probe_connectivity = sa.Column(sa.Boolean)
    # 'ping_target' is ip address of host that is pinged
    ping_target = sa.Column(sa.String(64))
    # 'ping_interval' is the time between pings
    ping_interval = sa.Column(sa.Integer)


class RouterHAGroup(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """Represents an HA group as used in VRRP, HSRP, and GLBP."""
    # 'ha_type' can be 'VRRP', 'HSRP', or 'GLBP'
    ha_type = sa.Column(sa.String(255))
    # 'group_identity'
    group_identity = sa.Column(sa.String(255))
    # 'virtual_port_id' is id of port used for virtual IP address
    virtual_port_id = sa.Column(sa.String(36), sa.ForeignKey('ports.id'),
                                primary_key=True)
    virtual_port = orm.relationship(
        models_v2.Port,
        primaryjoin='Port.id==RouterHAGroup.virtual_port_id')
    # 'extra_port_id' is id of port for user visible router's extra ip address
    extra_port_id = sa.Column(sa.String(36),
                              sa.ForeignKey('ports.id', ondelete='SET NULL'),
                              nullable=True)
    extra_port = orm.relationship(
        models_v2.Port,
        primaryjoin='Port.id==RouterHAGroup.extra_port_id')
    # 'subnet_id' is id of subnet that this HA group serves
    subnet_id = sa.Column(sa.String(36), sa.ForeignKey('subnets.id'),
                          nullable=True)
    # 'user_router_id' is id of router visible to the user
    user_router_id = sa.Column(sa.String(36),
                               sa.ForeignKey('routers.id'))
    # 'timers_config' holds timer specific configurations
    timers_config = sa.Column(sa.String(255))
    # 'tracking_config' holds tracking object specific configurations
    tracking_config = sa.Column(sa.String(255))
    # 'other_config' holds other method specific configurations
    other_config = sa.Column(sa.String(255))


class RouterRedundancyBinding(model_base.BASEV2):
    """Represents binding between an HA enabled router and its
    redundancy routers."""
    # 'redundancy_router_id' is id of a redundancy router
    redundancy_router_id = sa.Column(sa.String(36),
                                     sa.ForeignKey('routers.id',
                                                   ondelete='CASCADE'),
                                     primary_key=True)
#    redundancy_router = orm.relationship(
#        l3_db.Router,
#        primaryjoin='Router.id==RouterRedundancyBinding.redundancy_router_id')
    # 'priority' is the priority used in VRRP, HSRP, and GLBP
    priority = sa.Column(sa.Integer)
    # 'user_router_id' is id of router visible to the user
    user_router_id = sa.Column(sa.String(36),
                               sa.ForeignKey('routers.id'))
#    user_router = orm.relationship(
#        l3_db.Router,
#        primaryjoin='Router.id==RouterRedundancyBinding.user_router_id')


class HA_db_mixin(object):
    """Mixin class to support VRRP, HSRP, and GLBP based HA for routing."""

    router_view = "extension:ha:view"

    def _ensure_create_ha_compliant(self, router):
        """To be called in create_router() BEFORE router is
        created in DB.
        """
        if not attributes.is_attr_set(router[ha.HA_ENABLED]):
            router[ha.HA_ENABLED] = cfg.CONF.ha_enabled_by_default
        if router[ha.HA_ENABLED] and not cfg.CONF.ha_support_enabled:
            raise ha.HADisabled()
        if not router[ha.HA_ENABLED]:
            return
        if router.get('external_gateway_info') is None:
            raise ha.HAOnlyForGatewayRouters(
                msg="HA is only supported for routers with gateway."
                    "Please specify 'external_gateway_info'")
        if not attributes.is_attr_set(router.get(ha.TYPE)):
            router[ha.TYPE] = cfg.CONF.default_ha_mechanism
        if router[ha.TYPE] in cfg.CONF.disabled_ha_mechanisms:
            raise ha.HADisabledHAType(type=ha_type)
        if not attributes.is_attr_set(router.get(ha.REDUNDANCY_LEVEL)):
            router[ha.REDUNDANCY_LEVEL] = cfg.CONF.default_ha_redundancy_level
        if not attributes.is_attr_set(router.get(ha.PROBE_CONNECTIVITY)):
            router[ha.PROBE_CONNECTIVITY] = (
                cfg.CONF.connectivity_probing_enabled_by_default)
        if not attributes.is_attr_set(router.get(ha.PING_TARGET)):
            router[ha.PING_TARGET] = cfg.CONF.default_ping_target
        if not attributes.is_attr_set(router.get(ha.PING_INTERVAL)):
            router[ha.PING_INTERVAL] = cfg.CONF.default_ping_interval

    def _create_redundancy_routers(self, context, new_router,
                                   router_requested, ports=[],
                                   create_ha_groups=False):
        """To be called in create_router() AFTER router has been
        created in DB.
        """
        if (ha.HA_ENABLED not in router_requested or
                not router_requested[ha.HA_ENABLED]):
            new_router[ha.HA_ENABLED] = False
            return
        with context.session.begin(subtransactions=True):
            priority = DEFAULT_MASTER_PRIORITY
            r_ha_s = RouterHASetting(
                router_id=new_router['id'],
                ha_type=router_requested[ha.TYPE],
                redundancy_level=router_requested[ha.REDUNDANCY_LEVEL],
                priority=priority,
                probe_connectivity=router_requested[ha.PROBE_CONNECTIVITY],
                ping_target=router_requested[ha.PING_TARGET],
                ping_interval=router_requested[ha.PING_INTERVAL])
            if r_ha_s.ping_target is None:
                LOG.warning(_("Connectivity probing for high-availability is "
                              "enabled but ping target is not specified. "
                              "Please configure option \'%s\'."),
                            PING_TARGET_OPT_NAME)
            context.session.add(r_ha_s)
            self._add_redundancy_routers(context.elevated(), 0,
                                         router_requested[ha.REDUNDANCY_LEVEL],
                                         new_router, ports, create_ha_groups,
                                         r_ha_s)
            self._extend_router_dict_ha(context, new_router)

    def _ensure_update_ha_compliant(self, context, id, router):
        """To be called in update_router() BEFORE router has been
        updated in DB.
        """
        current = self.get_router(context, id)
        has_gateway = router.get('external_gateway_info',
                                 current['external_gateway_info'] is not None)
        ha_enabled = router.get(ha.HA_ENABLED,
                                current[ha.HA_ENABLED]
                                if ha.HA_ENABLED in current else False)
        if ha_enabled:
            if not cfg.CONF.ha_support_enabled:
                raise ha.HADisabled()
            elif not has_gateway:
                raise ha.HAOnlyForGatewayRouters(
                    msg="Cannot clear gateway when HA is enabled.")
            if (ha.TYPE in router and ha.TYPE in current and
                    router[ha.TYPE] != current[ha.TYPE]):
                raise ha.HATypeCannotBeChanged()
        #TODO(bob-melander): Do I need to ensure router has no floatingips?

    def _update_redundancy_routers(self, context, updated_router,
                                   update_specification):
        """To be called in update_router() AFTER router has been
        updated in DB.
        """
        router_requested = update_specification['router']
        r_has_db = self._get_ha_settings_by_router_id(context,
                                                      updated_router['id'])
        self._extend_router_dict_ha(context, updated_router, r_has_db)
        if not (updated_router[ha.HA_ENABLED] or
                router_requested.get(ha.HA_ENABLED, False)):
            # No HA currently enabled and no HA requested so we're done
            return
        e_context = context.elevated()
        with context.session.begin(subtransactions=True):
            if (not updated_router[ha.HA_ENABLED] and
                    router_requested.get(ha.HA_ENABLED, False)):
                # No HA currently enabled but HA requested
                router_requested['external_gateway_info'] = (
                    updated_router['external_gateway_info'])
                self._ensure_create_ha_compliant(router_requested)
                # The redundancy routers need interfaces on the
                # same networks as the user visible router.
                ports = self._get_router_interfaces(e_context,
                                                    updated_router['id'])
                self._create_redundancy_routers(context, updated_router,
                                                router_requested, ports,
                                                create_ha_groups=True)
                return
            rr_ids = self._get_redundancy_router_ids(context,
                                                     updated_router['id'])
            # The redundancy routers need interfaces on the
            # same networks as the user visible router.
            ports = self._get_router_interfaces(context,
                                                updated_router['id'])
            if (updated_router[ha.HA_ENABLED] and not router_requested.get(
                    ha.HA_ENABLED, updated_router[ha.HA_ENABLED])):
                # HA currently enabled but HA disable requested
                self._remove_redundancy_routers(e_context, rr_ids, ports)
                context.session.delete(r_has_db)
            else:
                # HA currently enabled and HA setting update (other than
                # disable HA) requested
                if ha.PROBE_CONNECTIVITY in router_requested:
                    r_has_db.probe_connectivity = (
                        router_requested[ha.PROBE_CONNECTIVITY])
                if ha.PING_TARGET in router_requested:
                    r_has_db.ping_target = router_requested[ha.PING_TARGET]
                if ha.PING_INTERVAL in router_requested:
                    r_has_db.ping_interval = router_requested[ha.PING_INTERVAL]
                old_redundancy_level = r_has_db.redundancy_level
                if ha.REDUNDANCY_LEVEL in router_requested:
                    diff = (router_requested[ha.REDUNDANCY_LEVEL] -
                            old_redundancy_level)
                    r_has_db.redundancy_level = (
                        router_requested[ha.REDUNDANCY_LEVEL])
                else:
                    diff = 0
                context.session.add(r_has_db)
                if diff < 0:
                    # Remove -diff redundancy routers
                    to_remove = rr_ids[len(rr_ids) + diff:]
                    rr_ids = rr_ids[:len(rr_ids) + diff]
                    self._remove_redundancy_routers(e_context, to_remove, ports)
                elif diff > 0:
                    # Add diff redundancy routers
                    start = old_redundancy_level + 1
                    stop = start + diff
                    self._add_redundancy_routers(e_context, start, stop,
                                                 updated_router, ports)
                # Notify redundancy routers about changes
                for r_id in rr_ids:
                    self.update_router(e_context, r_id, {'router': {}})

# Private function
    def _add_redundancy_routers(self, context, start_index, stop_index,
                                user_visible_router, ports=[],
                                create_ha_groups=False, ha_settings=None):
        """Creates a redundancy router and its interfaces on
        the specified subnets."""
        priority = (DEFAULT_MASTER_PRIORITY +
                    max(0, start_index - 1)*PRIORITY_INCREASE_STEP)
        r = copy.deepcopy(user_visible_router)
        # No tenant_id so redundant routers are hidden from user
        r['tenant_id'] = ''
        name = r['name']
        for i in xrange(start_index, stop_index):
            del r['id']
            # The redundant routers must have HA disabled
            r[ha.HA_ENABLED] = False
            r['name'] = name + REDUNDANCY_ROUTER_SUFFIX + str(max(1, i))
            r = super(HA_db_mixin, self).create_router(context.elevated(),
                                                       {'router': r})
            priority += PRIORITY_INCREASE_STEP
            r_b_b = RouterRedundancyBinding(
                redundancy_router_id=r['id'],
                priority=priority,
                user_router_id=user_visible_router['id'])
            context.session.add(r_b_b)
            for port in ports:
                # There should only be one ha group per network
                if create_ha_groups and i == start_index:
                    self._create_ha_group(context, user_visible_router['id'],
                                          port, ha_settings)
                ha_port = self._create_ha_port(context, port['network_id'], '')
                interface_info = {'port_id': ha_port['id']}
                self.add_router_interface(
                    context, r['id'], interface_info)
                pass

# Private function
    def _remove_redundancy_routers(self, context, router_ids, ports,
                                   delete_ha_groups=False):
        """Deletes all interfaces of the specified redundancy routers
        and then the redundancy routers themselves."""
        e_context = context.elevated()
        subnets_info = [{'subnet_id': port['fixed_ips'][0]['subnet_id']}
                        for port in ports]
        for r_id in router_ids:
            for i in xrange(len(subnets_info)):
                self.remove_router_interface(e_context, r_id, subnets_info[i])
                # There is only one ha group per network so only delete once
                if delete_ha_groups and r_id == router_ids[0]:
                    self._delete_ha_group(context, ports[i]['id'])
            self.delete_router(e_context, r_id)

# Private function
    def _get_router_interfaces(self, context, id):
        device_filter = {'device_id': [id],
                         'device_owner': [l3_db.DEVICE_OWNER_ROUTER_INTF]}
        return self.get_ports(context.elevated(), filters=device_filter)

    def _delete_redundancy_routers(self, context, router):
        """To be called in delete_router() BEFORE router has been
        deleted in DB. The router should have not interfaces.
        """
        self._extend_router_dict_ha(context, router)
        if not router.get(ha.HA_ENABLED, False):
            return
        e_context = context.elevated()
        router_ids = self._get_redundancy_router_ids(e_context, router['id'])
        for router_id in router_ids:
            self.delete_router(e_context, router_id)

    def _add_redundancy_router_interfaces(self, context, router_id, new_port):
        """To be called in add_router_interface() AFTER interface has been
        added to router in DB.
        """
        ha_settings = self._get_ha_settings_by_router_id(context, router_id)
        if ha_settings is None:
            return
        e_context = context.elevated()
        with context.session.begin(subtransactions=True):
            self._create_ha_group(e_context, router_id, new_port, ha_settings)
            for r_id in self._get_redundancy_router_ids(context, router_id):
                ha_port = self._create_ha_port(
                    e_context, new_port['network_id'], '')
                interface_info = {'port_id': ha_port['id']}
                self.add_router_interface(e_context, r_id, interface_info)

# Private function
    def _create_ha_group(self, context, router_id, port, ha_settings):
        ha_group_uuid = uuidutils.generate_uuid()
        # use HA group as device instead of the router to hide this port
        extra_port = self._create_ha_port(context, port['network_id'],
                                          ha_group_uuid)
        r_ha_g = RouterHAGroup(
            id=ha_group_uuid,
            tenant_id=self._get_tenant_id_for_create(context, port),
            ha_type=ha_settings['ha_type'],
            group_identity=self._generate_group_identity(
                ha_settings['ha_type'], router_id, ha_group_uuid),
            virtual_port_id=port['id'],
            extra_port_id=extra_port['id'],
            subnet_id=port['fixed_ips'][0]['subnet_id'],
            user_router_id=router_id,
            timers_config=self._get_default_time_config(router_id),
            tracking_config=self._get_default_tracking_config(router_id),
            other_config=self._get_default_other_config(router_id))
        context.session.add(r_ha_g)
        return r_ha_g

    def _remove_redundancy_router_interfaces(self, context, router_id,
                                             old_port):
        """To be called in delete_router_interface() BEFORE interface has been
        removed from router in DB.
        """
        ha_settings = self._get_ha_settings_by_router_id(context, router_id)
        if ha_settings is None or old_port is None:
            return
        interface_info = {
            'subnet_id': old_port['fixed_ips'][0]['subnet_id']}
        e_context = context.elevated()
        with context.session.begin(subtransactions=True):
            for r_id in self._get_redundancy_router_ids(context, router_id):
                self.remove_router_interface(e_context, r_id, interface_info)
            self._delete_ha_group(e_context, old_port['id'])

    # Private function
    def _delete_ha_group(self, context, virtual_port_id):
        hag = self._get_ha_group_by_virtual_port_id(context, virtual_port_id)
        if hag is not None:
            self.delete_port(context, hag.extra_port_id, l3_port_check=False)
            context.session.delete(hag)

    def _extend_router_dict_ha(self, context, router, ha_s=None):
        if ha_s is None:
            ha_s = self._get_ha_settings_by_router_id(context, router['id'])
        # We only add HA attributes to the router visible to the user.
        router[ha.HA_ENABLED] = False if ha_s is None else True
        if router[ha.HA_ENABLED] and self._check_view_auth(
                context, router, self.router_view):
            router[ha.TYPE] = ha_s.ha_type
            router[ha.REDUNDANCY_LEVEL] = ha_s.redundancy_level
            router[ha.PROBE_CONNECTIVITY] = ha_s.probe_connectivity
            if router[ha.PROBE_CONNECTIVITY]:
                router[ha.PING_TARGET] = ha_s.ping_target
                router[ha.PING_INTERVAL] = ha_s.ping_interval
        return router

    def _populate_ha_information(self, context, router):
        """To be called when router information, including router interface
        list, (for the l3_cfg_agent) has been collected so it is extended
        with ha information."""
        r_r_b = self._get_redundancy_router_bindings(
            context, redundancy_router_id=router['id'])
        if not r_r_b:
            # The router is a user visible router. It MAY or
            # MAY NOT have HA enabled.
            user_router_id = router['id']
            fips = []
        else:
            user_router_id = r_r_b[0].user_router_id
            # Need to fetch floatingips configrations from user visible router
            # so they can be added to the redundancy routers.
            fips = self.get_floatingips(context,
                                        {'router_id': [user_router_id]})
        ha_s = self._get_ha_settings_by_router_id(context, user_router_id)
        if ha_s is None:
            # Router does not have HA enabled
            router['ha_info'] = {ha.HA_ENABLED: False}
            return
        # We add the HA settings from user visible router to
        # its redundancy routers.
        ha_dict = {}
        self._extend_router_dict_ha(context, ha_dict, ha_s)
        ha_dict['priority'] = r_r_b[0].priority if r_r_b else ha_s.priority
        router['ha_info'] = ha_dict
        hags = self._get_subnet_id_indexed_ha_groups(context, user_router_id)
        # The interfaces of the user visible router must use the
        # IP configuration of the extra ports in the HA groups.
        modified_interfaces = []
        e_context = context.elevated()
        for itfc in router.get(l3_constants.INTERFACE_KEY, []):
            hag = hags[itfc['fixed_ips'][0]['subnet_id']]
            if router['id'] == hag.user_router_id:
                router_port = self.get_port(e_context, hag.extra_port_id)
                self._populate_subnet_for_ports(e_context, [router_port])
                modified_interfaces.append(router_port)
                virtual_port = itfc
            else:
                virtual_port = self.get_port(context, hag.virtual_port_id)
                self._populate_subnet_for_ports(context, [virtual_port])
                router_port = itfc
            ha_g_info = {
                'ha_type': hag.ha_type,
                'group': hag.group_identity,
                'timers_config': hag.timers_config,
                'tracking_config': hag.tracking_config,
                'other_config': hag.other_config,
                'virtual_port': virtual_port
                }
            router_port['ha_info'] = ha_g_info
        if modified_interfaces:
            router[l3_constants.INTERFACE_KEY] = modified_interfaces
        if fips:
            router[l3_constants.FLOATINGIP_KEY] = fips

########## "Private" functions defined below

    def _create_ha_port(self, context, network_id, device_id):
        """Creates ports used specially for HA purposes.
        """
        return self.create_port(context, {
            'port':
            {'tenant_id': '',  # intentionally not set
             'network_id': network_id,
             'mac_address': attributes.ATTR_NOT_SPECIFIED,
             'fixed_ips': attributes.ATTR_NOT_SPECIFIED,
             'device_id': device_id,
             'device_owner': DEVICE_OWNER_ROUTER_INTF,
             'admin_state_up': True,
             'name': ''}})

    def _get_ha_settings_by_router_id(self, context, id):
        query = context.session.query(RouterHASetting)
        query = query.filter(RouterHASetting.router_id == id)
        try:
            r_ha_s = query.one()
        except exc.NoResultFound, exc.MultipleResultsFound:
            return
        return r_ha_s

    def _get_ha_group_by_virtual_port_id(self, context, id):
        query = context.session.query(RouterHAGroup)
        query = query.filter(RouterHAGroup.virtual_port_id == id)
        try:
            r_ha_g = query.one()
        except exc.NoResultFound, exc.MultipleResultsFound:
            return
        return r_ha_g

    def _get_subnet_id_indexed_ha_groups(self, context, router_id,
                                         load_virtual_port=False):
        query = context.session.query(RouterHAGroup)
        query = query.filter(RouterHAGroup.user_router_id == router_id)
        if load_virtual_port:
            query = query.options(joinedload('redundancy_router'))
        return {hag['subnet_id']: hag for hag in query.all()}

    def _get_redundancy_router_bindings(self, context, router_id=None,
                                        redundancy_router_id=None):
        query = context.session.query(RouterRedundancyBinding)
        if router_id is not None:
            query = query.filter(
                RouterRedundancyBinding.user_router_id == router_id)
        if redundancy_router_id is not None:
            query = query.filter(
                RouterRedundancyBinding.redundancy_router_id ==
                redundancy_router_id)
        query = query.order_by(RouterRedundancyBinding.priority)
        return query.all()

    def _get_redundancy_router_ids(self, context, router_id):
        return [binding.redundancy_router_id for binding in
                self._get_redundancy_router_bindings(context,
                                                     router_id=router_id)]

    def _generate_group_identity(self, ha_type, router_id, ha_group_id):
        #TODO(bob-melander): Generate "guaranteed" unique id
        if ha_type == ha.HA_HSRP:
            return random.randint(0, MAX_HSRP_GROUPS)
        elif ha_type == ha.HA_VRRP:
            return random.randint(0, MAX_VRRP_GROUPS)
        else:
            # ha_type must be ha_type.GLBP
            return random.randint(0, MAX_GLBP_GROUPS)

    def _get_default_time_config(self, router):
        return ''

    def _get_default_tracking_config(self, router):
        return ''

    def _get_default_other_config(self, router):
        return ''