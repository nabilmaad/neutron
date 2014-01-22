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

import string

import eventlet
from oslo.config import cfg
import sqlalchemy as sa
from sqlalchemy import and_
from sqlalchemy import orm
from sqlalchemy import func
from sqlalchemy.orm import exc
from sqlalchemy.orm import joinedload

from keystoneclient.v2_0 import client as k_client
from keystoneclient import exceptions as k_exceptions

from quantum.api.v2 import attributes
#from quantum.api.rpc.agentnotifiers import l3_rpc_agent_api
from quantum import context as q_context
#from quantum.common import utils
from quantum.common import constants as l3_constants
from quantum.common import exceptions as q_exc
from quantum.db import agents_db
from quantum.db import extraroute_db
from quantum.db import l3_db
from quantum.db import model_base
from quantum.db import models_v2
from quantum.extensions import providernet as pr_net
from quantum.plugins.cisco.common import config
from quantum.plugins.cisco.extensions import n1kv_profile
from quantum.plugins.cisco.l3.common import service_vm_lib
from quantum.plugins.cisco.l3.common import l3_rpc_joint_agent_api
from quantum.plugins.cisco.l3.common import constants as cl3_const
from quantum import manager
from quantum.openstack.common import log as logging
from quantum.openstack.common import timeutils
from quantum.openstack.common import uuidutils


LOG = logging.getLogger(__name__)


#TODO(bob-melander): Update this for the N1kv plugin
TRUNKED_NETWORKS = 'trunked_networks'

#TODO(bob-melander): Revisit these configurations to remove
#some if possible
router_appliance_opts = [
    cfg.StrOpt('l3_admin_tenant', default='L3AdminTenant',
               help=_("Name of the L3 admin tenant")),
    cfg.StrOpt('default_router_type', default='CSR1kv',
               help=_("Default type of router to create")),
    cfg.IntOpt('csr1kv_flavor', default=621,
               help=_("Name or UUID of Nova flavor used for CSR1kv VM")),
    cfg.StrOpt('csr1kv_image', default='csr1kv_openstack_img',
               help=_("Name or UUID of Glance image used for CSR1kv VM")),
    cfg.StrOpt('management_port_profile', default='osn_mgmt_pp',
               help=_("Name of N1kv port profile for management ports")),
    cfg.StrOpt('t1_port_profile', default='osn_t1_pp',
               help=_("Name of N1kv port profile for T1 ports")),
    cfg.StrOpt('t2_port_profile', default='osn_t2_pp',
               help=_("Name of N1kv port profile for T2 ports")),
    cfg.StrOpt('t1_network_profile', default='osn_t1_np',
               help=_("Name of N1kv network profile for T1 networks")),
    cfg.StrOpt('t2_network_profile', default='osn_t2_np',
               help=_("Name of N1kv network profile for T2 networks")),
    cfg.StrOpt('management_network', default='osn_mgmt_nw',
               help=_("Name of management network for CSR VM configuration")),
    cfg.StrOpt('default_security_group', default='mgmt_sec_grp',
               help=_("Default security group applied on mgmt port")),
    cfg.StrOpt('hosting_scheduler_driver',
               default='quantum.plugins.cisco.l3.scheduler.'
                       'l3_hosting_entity_scheduler.L3HostingEntityScheduler',
               help=_('Driver to use for scheduling router to a hosting '
                      'entity')),
    cfg.IntOpt('cfg_agent_down_time', default=10,
               help=_('Seconds of no status update until a cfg agent '
                      'is considered down.')),
    cfg.IntOpt('max_routers_per_csr1kv', default=1,
               help=_("The maximum number of logical routers a CSR1kv VM "
                      "instance will host")),
    cfg.IntOpt('standby_pool_size', default=1,
               help=_("The number of running CSR1kv VMs to maintain "
                      "as a pool of standby VMs")),
    cfg.IntOpt('csr1kv_booting_time', default=420,
               help=_("The time in seconds it typically takes to "
                      "boot a CSR1kv VM")),
    cfg.StrOpt('csr1kv_user_name', default='stack',
               help=_("User name to login to CSR1kv VMs")),
    cfg.StrOpt('csr1kv_password', default='cisco',
               help=_("Password to login to CSR1kv VMs")),
    cfg.StrOpt('templates_path',
               default='/opt/stack/data/quantum/cisco/templates',
               help=_("Path to default templates")),
    cfg.StrOpt('csr_config_template', default='csr_cfg_template',
               help=_("CSR default template file name")),
    cfg.StrOpt('service_vm_config_path',
               default='/opt/stack/data/quantum/cisco/config_drive',
               help=_("Path to config drive files for service VMs")),
    cfg.IntOpt('max_routers_per_N7k', default=1000,
               help=_("The maximum number of logical routers a Nexus 7k "
                      "switch will host")),
]

# Segmentation types
VLAN_SEGMENTATION = 'VLAN'

MIN_LL_VLAN_TAG = 10
MAX_LL_VLAN_TAG = 200
FULL_VLAN_SET = set(range(MIN_LL_VLAN_TAG, MAX_LL_VLAN_TAG))

# Port lookups can fail so retries are needed
MAX_HOSTING_PORT_LOOKUP_ATTEMPTS = 10
SECONDS_BETWEEN_HOSTING_PORT_LOOKSUPS = 2

cfg.CONF.register_opts(router_appliance_opts)

class RouterCreateInternalError(q_exc.QuantumException):
    message = _("Router could not be created due to internal error.")


class RouterInternalError(q_exc.QuantumException):
    message = _("Internal error during router processing.")


class RouterBindingInfoError(q_exc.QuantumException):
    message = _("Could not get binding information for router %(router_id)s.")


class HostingEntity(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """Represents an appliance hosting OsN router(s). When the
       hosting entity is a Nova VM 'id' is uuid of that OsC VM.
    """
    __tablename__ = 'hostingentities'

    admin_state_up = sa.Column(sa.Boolean, nullable=False, default=True)
    # 'host_type' can be 'NetworkNamespaceNode', 'CSR1kv', ...
    host_type = sa.Column(sa.String(255), nullable=False)
    # 'ip_address' is address of hosting entity's management interface
    ip_address = sa.Column(sa.String(64), nullable=False)
    # 'transport_port' is udp/tcp port of hosting entity. May be empty.
    transport_port = sa.Column(sa.Integer)
    l3_cfg_agent_id = sa.Column(sa.String(36),
                                sa.ForeignKey('agents.id'),
                                nullable=True)
    l3_cfg_agent = orm.relationship(agents_db.Agent)
    # Service VMs take time to boot so we store creation time
    # so we can give preference to older ones when scheduling
    created_at = sa.Column(sa.DateTime, nullable=False)
    # Typical time (in seconds) needed for hosting entity to boot
    # into operational state.
    booting_time = sa.Column(sa.Integer, default=0)
    status = sa.Column(sa.String(16))
    # 'tenant_bound' is empty or is id of the only tenant allowed to
    # own/place resources on this hosting entity
    tenant_bound = sa.Column(sa.String(255))
    auto_delete_on_fail = sa.Column(sa.Boolean, default=True, nullable=False)


class RouterHostingEntityBinding(model_base.BASEV2):
    """Represents binding between OsN routers and
       their hosting entities.
    """
    router_id = sa.Column(sa.String(36),
                          sa.ForeignKey('routers.id', ondelete='CASCADE'),
                          primary_key=True)
    router = orm.relationship(l3_db.Router)
    # 'router_type' can be 'NetworkNamespace', 'CSR1kv', ...
    router_type = sa.Column(sa.String(255), nullable=False)
    # If 'auto_schedule' is True then router is automatically scheduled
    # if it lacks a hosting entity or its hosting entity fails.
    auto_schedule = sa.Column(sa.Boolean, default=True, nullable=False)
    share_hosting_entity = sa.Column(sa.Boolean, default=True, nullable=False)
    hosting_entity_id = sa.Column(sa.String(36),
                                  sa.ForeignKey('hostingentities.id',
                                                ondelete='SET NULL'))
    hosting_entity = orm.relationship(HostingEntity)


class TrunkInfo(model_base.BASEV2):
    """Represents trunking info for a router port."""
    router_port_id = sa.Column(sa.String(36),
                               sa.ForeignKey('ports.id',
                                             ondelete="CASCADE"),
                               primary_key=True)
    router_port = orm.relationship(models_v2.Port,
                                   primaryjoin='Port.id==TrunkInfo.'
                                               'router_port_id',
                                   backref=orm.backref('trunk_info',
                                                       cascade='all',
                                                       uselist=False))
    # type of network the router port belongs to
    network_type = sa.Column(sa.String(32))
    hosting_port_id = sa.Column(sa.String(36),
                                sa.ForeignKey('ports.id',
                                              ondelete='CASCADE'))
    hosting_port = orm.relationship(models_v2.Port,
                                    primaryjoin='Port.id==TrunkInfo.'
                                                'hosting_port_id')
    segmentation_tag = sa.Column(sa.Integer,
                                 autoincrement=False)


class L3_router_appliance_db_mixin(extraroute_db.ExtraRoute_db_mixin):
    """ Mixin class to support router appliances to implement Quantum's
        L3 routing functionality """

    # TODO(bobmel): Set to correct value during start of Quantum
    _plugin = cl3_const.N1KV_PLUGIN

    _svc_vm_mgr = None
    _l3_tenant_uuid = None

    _n1kv_mgmt_pp_id = None
    _n1kv_t1_pp_id = None
    _n1kv_t2_pp_id = None
    _n1kv_t1_np_id = None
    _n1kv_t2_np_id = None

    _mgmt_nw_uuid = None
    _csr_mgmt_sec_grp_id = None

    hosting_scheduler = None

    @classmethod
    def svc_vm_mgr(cls):
        if cls._svc_vm_mgr is None:
            auth_url = (cfg.CONF.keystone_authtoken.auth_protocol + "://" +
                        cfg.CONF.keystone_authtoken.auth_host + ":" +
                        str(cfg.CONF.keystone_authtoken.auth_port) + "/v2.0")
            username = cfg.CONF.keystone_authtoken.admin_user
            pw = cfg.CONF.keystone_authtoken.admin_password
            tenant = cfg.CONF.l3_admin_tenant
            cls._svc_vm_mgr = service_vm_lib.ServiceVMManager(
                user=username, passwd=pw, l3_admin_tenant=tenant,
                auth_url=auth_url)
        return cls._svc_vm_mgr

    @classmethod
    def l3_tenant_id(cls):
        if cls._l3_tenant_uuid is None:
            auth_url = (cfg.CONF.keystone_authtoken.auth_protocol + "://" +
                        cfg.CONF.keystone_authtoken.auth_host + ":" +
                        str(cfg.CONF.keystone_authtoken.auth_port) + "/v2.0")
            user = cfg.CONF.keystone_authtoken.admin_user
            pw = cfg.CONF.keystone_authtoken.admin_password
            tenant = cfg.CONF.keystone_authtoken.admin_tenant_name
            keystone = k_client.Client(username=user, password=pw,
                                       tenant_name=tenant,
                                       auth_url=auth_url)
            try:
                tenant = keystone.tenants.find(name=cfg.CONF.l3_admin_tenant)
                cls._l3_tenant_uuid = tenant.id
            except k_exceptions.NotFound:
                LOG.error(_('No tenant with a name or ID of %s exists.'),
                            cfg.CONF.l3_admin_tenant)
            except k_exceptions.NoUniqueMatch:
                LOG.error(_('Multiple tenants matches found for %s'),
                            cfg.CONF.l3_admin_tenant)
        return cls._l3_tenant_uuid

    @classmethod
    def _get_profile_id(cls, p_type, resource, name):
        tenant_id = cls.l3_tenant_id()
        if not tenant_id:
            return
        if p_type == 'net_profile':
            profiles = manager.QuantumManager.get_plugin().get_network_profiles(
                q_context.get_admin_context(),
                {'tenant_id': [tenant_id],
                 'name': [name]},
                ['id'])
        else:
            profiles = manager.QuantumManager.get_plugin().get_policy_profiles(
                q_context.get_admin_context(),
                {'tenant_id': [tenant_id],
                 'name': [name]},
                ['id'])
        if len(profiles) == 1:
            return profiles[0]
        elif len(profiles) > 1:
            # Profile must have a unique name.
            LOG.error(_('The %(resource)s %(name)s does not have unique name. '
                        'Please refer to admin guide and create one.'),
                      {'resource': resource, 'name': name})
        else:
            # Profile has not been created.
            LOG.error(_('There is no %(resource)s %(name)s. Please refer to '
                        'admin guide and create one.'),
                      {'resource': resource, 'name': name})

    @classmethod
    def n1kv_mgmt_pp_id(cls):
        if cls._n1kv_mgmt_pp_id is None:
            cls._n1kv_mgmt_pp_id = cls._get_profile_id(
                'port_profile', 'N1kv port profile',
                cfg.CONF.management_port_profile)
        return cls._n1kv_mgmt_pp_id

    @classmethod
    def n1kv_t1_pp_id(cls):
        if cls._n1kv_t1_pp_id is None:
            cls._n1kv_t1_pp_id = cls._get_profile_id(
                'port_profile', 'N1kv port profile', cfg.CONF.t1_port_profile)
        return cls._n1kv_t1_pp_id

    @classmethod
    def n1kv_t2_pp_id(cls):
        if cls._n1kv_t2_pp_id is None:
            cls._n1kv_t2_pp_id = cls._get_profile_id(
                'port_profile', 'N1kv port profile', cfg.CONF.t2_port_profile)
        return cls._n1kv_t2_pp_id

    @classmethod
    def n1kv_t1_np_id(cls):
        if cls._n1kv_t1_np_id is None:
            cls._n1kv_t1_np_id = cls._get_profile_id(
                'net_profile', 'N1kv network profile',
                cfg.CONF.t1_network_profile)
        return cls._n1kv_t1_np_id

    @classmethod
    def n1kv_t2_np_id(cls):
        if cls._n1kv_t2_np_id is None:
            cls._n1kv_t2_np_id = cls._get_profile_id(
                'net_profile', 'N1kv network profile',
                cfg.CONF.t2_network_profile)
        return cls._n1kv_t2_np_id

    @classmethod
    def mgmt_nw_id(cls):
        if cls._mgmt_nw_uuid is None:
            tenant_id = cls.l3_tenant_id()
            if not tenant_id:
                return None
            net = manager.QuantumManager.get_plugin().get_networks(
                q_context.get_admin_context(),
                {'tenant_id': [tenant_id],
                 'name': [cfg.CONF.management_network]},
                ['id', 'subnets'])
            if len(net) == 1:
                num_subnets = len(net[0]['subnets'])
                if num_subnets == 0:
                    LOG.error(_('The virtual management network has no'
                                'subnet. Please refer to admin guide and '
                                'assign one'))
                    return
                elif num_subnets > 1:
                     LOG.info(_('The virtual management network has %s'
                                'subnets. The first one will be used.'),
                              num_subnets)
                cls._mgmt_nw_uuid = net[0].get('id')
            elif len(net) > 1:
                # Management network must have a unique name.
                LOG.error(_('The virtual management network for CSR1kv VMs '
                            'does not have unique name. Please refer to '
                            'admin guide and create one.'))
            else:
                # Management network has not been created.
                LOG.error(_('There is no virtual management network for '
                            'CSR1kv VMs. Please refer to admin guide and '
                            'create one.'))
        return cls._mgmt_nw_uuid

    @classmethod
    def csr_mgmt_sec_grp_id(cls):
        if cls._plugin == cl3_const.N1KV_PLUGIN:
            return None
        if cls._csr_mgmt_sec_grp_id is None:
            # Get the id for the csr_mgmt_security_group_id
            tenant_id = cls.l3_tenant_id()
            res = manager.QuantumManager.get_plugin().get_security_groups(
                q_context.get_admin_context(),
                {'tenant_id': [tenant_id],
                 'name': [cfg.CONF.default_security_group]},
                ['id'])
            if len(res) == 1:
                sec_grp_id = res[0].get('id', None)
                cls._csr_mgmt_sec_grp_id = sec_grp_id
            elif len(res) > 1:
                # CSR Mgmt sec group must be unique.
                LOG.error(_('The security group for csr mgmt '
                            'does not have unique name. Please refer to '
                            'admin guide and create one.'))
            else:
                # CSR Mgmt security group is not present.
                LOG.error(_('There is no security group for csr mgmt. '
                            'Please refer to admin guide and '
                            'create one.'))
        return cls._csr_mgmt_sec_grp_id

    def create_router(self, context, router):
        r = router['router']
        # Bob: Hard coding router type for now
        r['router_type'] = cfg.CONF.default_router_type
#        r['router_type'] = cl3_const.HARDWARE_ROUTER_TYPE
        r['share_host'] = True
        if (r['router_type'] != cl3_const.NAMESPACE_ROUTER_TYPE and
                self.mgmt_nw_id()) is None:
            raise RouterCreateInternalError()
        self._ensure_create_ha_compliant(r)
        with context.session.begin(subtransactions=True):
            router_created = (super(L3_router_appliance_db_mixin, self).
                              create_router(context, router))
            r_he_b_db = RouterHostingEntityBinding(
                router_id=router_created['id'],
                router_type=r.get('router_type',
                                  cfg.CONF.default_router_type),
                auto_schedule=r.get('auto_schedule',
                                    cfg.CONF.router_auto_schedule),
                share_hosting_entity=r.get('share_host', True),
                hosting_entity_id=None)
            context.session.add(r_he_b_db)
            # process any HA
            self._create_redundancy_routers(context, router_created, r)
        return router_created

    def update_router(self, context, id, router):
        r = router['router']
        # Ensure update is compliant with any HA
        self._ensure_update_ha_compliant(context, id, r)
        # Check if external gateway has changed so we may have to
        # update trunking
        new_ext_gw = r.get('external_gateway_info', {}).get('network_id', '')
        o_r_db = self._get_router(context, id)
        if o_r_db.gw_port is None:
            old_ext_gw = ''
            old_trunk_nw_id = None
            old_hosting_port_id = None
            old_hosting_port_name = ''
        else:
            old_ext_gw = o_r_db.gw_port.network_id
            old_hosting_port_id, old_trunk_nw_id = (
                self._get_trunk_port_and_network_ids(context, o_r_db.gw_port))
            old_hosting_port_name = self._get_hosting_port_name(
                context, o_r_db.gw_port.network_id)
        #TODO(bobmel): Check if 'is None' test is really needed
        ext_gateway_changed = (
            False if (old_ext_gw == new_ext_gw or
                      r.get('external_gateway_info') is None)
            else True)
        router_updated = (
            super(L3_router_appliance_db_mixin, self).update_router(context,
                                                                    id,
                                                                    router))
        # process any HA
        self._update_redundancy_routers(context, router_updated, router)
        gw_change_status = {'changed': ext_gateway_changed,
                            'old_hosting_port_name': old_hosting_port_name,
                            'old_hosting_port_id': old_hosting_port_id,
                            'old_trunk_nw_id': old_trunk_nw_id}
        routers = self.get_sync_data_ext(context.elevated(), [o_r_db['id']],
                                         ext_gw_change_status=gw_change_status)
        l3_rpc_joint_agent_api.L3JointAgentNotify.routers_updated(context,
                                                                  routers)
        return router_updated

    def delete_router(self, context, id):
        # Collect info needed after parent has deleted router
        r_he_b = self._get_router_binding_info(context, id)
        router = self._make_router_dict(r_he_b.router)
        self._add_type_and_hosting_info(context, router,
                                        binding_info=r_he_b,
                                        schedule=False)
        hosting_port_id, trunk_nw_id = (
            self._get_trunk_port_and_network_ids(context,
                                                 r_he_b.router.gw_port))
        hosting_entity = r_he_b.hosting_entity
        # process any HA
        self._delete_redundancy_routers(context, router)
        super(L3_router_appliance_db_mixin, self).delete_router(context, id)
        if router['router_type'] != cl3_const.NAMESPACE_ROUTER_TYPE:
            self._cleanup_gateway_configurations(context, router, trunk_nw_id)
            self.hosting_scheduler.unschedule_router_from_hosting_entity(
                self, context, router, hosting_entity)
        l3_rpc_joint_agent_api.L3JointAgentNotify.router_deleted(context,
                                                                 router)

    def get_router(self, context, id, fields=None):
        router = super(L3_router_appliance_db_mixin, self).get_router(context,
                                                                      id)
        # process any HA
        self._extend_router_dict_ha(context, router)
        #Todo(bob-melander): Create router type extension to publish attributes
        self._add_type_and_hosting_info(context, router, binding_info=None,
                                        schedule=False)
        return self._fields(router, fields)

    def get_routers(self, context, filters=None, fields=None,
                    sorts=None, limit=None, marker=None,
                    page_reverse=False):
        routers = super(L3_router_appliance_db_mixin, self).get_routers(
            context, filters, fields=None, sorts=sorts, limit=limit,
            marker=marker, page_reverse=page_reverse)
        res = []
        for router in routers:
            # process any HA
            self._extend_router_dict_ha(context, router)
            #Todo(bob-melander): Create router type extension to
            # publish attributes
            self._add_type_and_hosting_info(context, router, binding_info=None,
                                            schedule=False)
            res.append(self._fields(router, fields))
        return res

    def _cleanup_gateway_configurations(self, context, router, trunk_nw_id):
        if router['router_type'] != cl3_const.CSR_ROUTER_TYPE:
            return
        if trunk_nw_id is None:
            return
        self._update_trunking_on_hosting_port(context, trunk_nw_id, {})

    def add_router_interface(self, context, router_id, interface_info):
        info = (super(L3_router_appliance_db_mixin, self).
                add_router_interface(context, router_id, interface_info))
        if_chg_status = {'changed': True,
                         'old_hosting_port_name': '',
                         'old_hosting_port_id': None,
                         'old_trunk_nw_id': None}
        new_port_db = self._get_port(context, info['port_id'])
        # process any HA
        self._add_redundancy_router_interfaces(context, router_id, new_port_db)
        routers = self.get_sync_data_ext(context.elevated(), [router_id],
                                         int_if_change_status=if_chg_status)
        l3_rpc_joint_agent_api.L3JointAgentNotify.routers_updated(
            context, routers, 'add_router_interface',
            {'network_id': new_port_db['network_id'],
             'subnet_id': info['subnet_id']})
        return info

    def remove_router_interface(self, context, router_id, interface_info):
        if not interface_info:
            msg = "Either subnet_id or port_id must be specified"
            raise q_exc.BadRequest(resource='router', msg=msg)
        if 'port_id' in interface_info:
            port_db = self._get_port(context, interface_info['port_id'])
            net_id = port_db['network_id']
            subnet_id = port_db['fixed_ips'][0]['subnet_id']
        elif 'subnet_id' in interface_info:
            subnet_db = self._get_subnet(context, interface_info['subnet_id'])
            subnet_id = subnet_db['id']
            port_db = self._get_router_port_db_on_subnet(context, router_id,
                                                         subnet_db)
            net_id = subnet_db['network_id']
        else:
            msg = "Either subnet_id or port_id must be specified"
            raise q_exc.BadRequest(resource='router', msg=msg)

        hosting_port_id, trunk_network_id = (
            self._get_trunk_port_and_network_ids(context, port_db))
        if hosting_port_id is None:
            if_chg_status = None
        else:
            if_chg_status = {
                'changed': True,
                'old_hosting_port_name': self._get_hosting_port_name(
                    context.elevated(), trunk_network_id),
                'old_hosting_port_id': hosting_port_id,
                'old_trunk_nw_id': trunk_network_id}
        # process any HA
        self._remove_redundancy_router_interfaces(context, router_id, port_db)
        info = (super(L3_router_appliance_db_mixin, self).
                remove_router_interface(context, router_id, interface_info))
        routers = self.get_sync_data_ext(context.elevated(), [router_id],
                                         int_if_change_status=if_chg_status)
        l3_rpc_joint_agent_api.L3JointAgentNotify.routers_updated(
            context, routers, 'remove_router_interface',
            {'network_id': net_id,
             'subnet_id': subnet_id})
        return info

    def create_floatingip(self, context, floatingip):
        info = super(L3_router_appliance_db_mixin, self).create_floatingip(
            context, floatingip)
        if info['router_id']:
            routers = self.get_sync_data_ext(context.elevated(),
                                             [info['router_id']])
            l3_rpc_joint_agent_api.L3JointAgentNotify.routers_updated(
                context, routers, 'create_floatingip')
        return info

    def update_floatingip(self, context, id, floatingip):
        orig_fl_ip = super(L3_router_appliance_db_mixin, self).get_floatingip(
            context, id)
        before_router_id = orig_fl_ip['router_id']
        info = super(L3_router_appliance_db_mixin, self).update_floatingip(
            context, id, floatingip)

        router_ids = []
        if before_router_id:
            router_ids.append(before_router_id)
        router_id = info['router_id']
        if router_id and router_id != before_router_id:
            router_ids.append(router_id)
        routers = self.get_sync_data_ext(context.elevated(), router_ids)
        l3_rpc_joint_agent_api.L3JointAgentNotify.routers_updated(
            context, routers, 'update_floatingip')
        return info

    def delete_floatingip(self, context, id):
        floatingip_db = self._get_floatingip(context, id)
        router_id = floatingip_db['router_id']
        super(L3_router_appliance_db_mixin, self).delete_floatingip(
            context, id)
        if router_id:
            routers = self.get_sync_data_ext(context.elevated(), [router_id])
            l3_rpc_joint_agent_api.L3JointAgentNotify.routers_updated(
                context, routers, 'delete_floatingip')

    def disassociate_floatingips(self, context, port_id):
        with context.session.begin(subtransactions=True):
            try:
                fip_qry = context.session.query(l3_db.FloatingIP)
                floating_ip = fip_qry.filter_by(fixed_port_id=port_id).one()
                router_id = floating_ip['router_id']
                floating_ip.update({'fixed_port_id': None,
                                    'fixed_ip_address': None,
                                    'router_id': None})
            except exc.NoResultFound:
                return
            except exc.MultipleResultsFound:
                # should never happen
                raise Exception(_('Multiple floating IPs found for port %s')
                                % port_id)
        if router_id:
            routers = self.get_sync_data_ext(context.elevated(), [router_id])
            l3_rpc_joint_agent_api.L3JointAgentNotify.routers_updated(context,
                                                                      routers)

    def get_router_type(self, context, id):
        r_he_b = self._get_router_binding_info(context, id, load_he_info=False)

        return r_he_b.router_type

    #TODO(bob-melander): Refactor - to new resource management class
    def register_hardware_hosting_entities(self):
        """To be called late during plugin initialization so that any
        Nexus hardware devices are registered as hosting entities."""
        _dev_dict = config.get_device_dictionary()
        devices = {}
        name_mapping = {
            'ssh_port': 'transport_port',
            'device_type': 'host_type',
            cl3_const.SUPPORTS_ROUTING: cl3_const.SUPPORTS_ROUTING,
            #TODO(bob-melander): handle these too:
            #'user_name': 'user_name',
            #'password': 'password',
        }
        for (dev_id, dev_ip, dev_key), v in _dev_dict.items():
            if dev_key in name_mapping.keys():
                val = (v if dev_key != cl3_const.SUPPORTS_ROUTING else
                       attributes.convert_to_boolean(v))
                if dev_ip not in devices:
                    devices[dev_ip] = {name_mapping[dev_key]: val}
                else:
                    devices[dev_ip][name_mapping[dev_key]] = val
        birth_date = timeutils.utcnow()
        context = q_context.get_admin_context()
        with context.session.begin(subtransactions=True):
            for device_ip, he_data in devices.items():
                if he_data.get('host_type') not in [cl3_const.NEXUS_AGGR_HOST,
                                                    cl3_const.NEXUS_TOR_HOST]:
                    continue
                he_db = self.get_hosting_entity_by_ip_address(context,
                                                              device_ip)
                if he_data.get(cl3_const.SUPPORTS_ROUTING, False):
                    if he_db is None:
                        he_db = HostingEntity(
                            id=uuidutils.generate_uuid(),
                            # intentionally left empty to make invisible to user
                            tenant_id='',
                            admin_state_up=True,
                            host_type=he_data['host_type'],
                            ip_address=device_ip,
                            transport_port=he_data.get(
                                'transport_port',
                                cl3_const.NEXUS_SSH_NETCONF_PORT),
                            l3_cfg_agent_id=None,
                            created_at=birth_date,
                            booting_time=cl3_const.NEXUS_BOOTING_TIME,
                            status=None,
                            tenant_bound=None,
                            auto_delete_on_fail=False)
                        context.session.add(he_db)
                    else:
                        he_db.update(he_data)
                elif he_db is not None:
                    context.session.delete(he_db)

    #TODO(bob-melander): Refactor - to new resource management class
    def get_hosting_entity_by_ip_address(self, context, ip_address):
        query = context.session.query(HostingEntity)
        query = query.filter(HostingEntity.ip_address == ip_address)
        try:
            he_db = query.one()
        except exc.NoResultFound, exc.MultipleResultsFound:
            return
        return he_db

    #TODO(bob-melander): Refactor - to new resource management class
    def create_csr1kv_vm_hosting_entities(self, context, num,
                                          tenant_bound=None):
        """Creates a number of CSR1kv VM instances that will act as
        routing service VM. These hosting entities can be bound to
        a certain tenant or for shared use. A list with the created
        hosting entity CSR1kv VMs is returned.
        """
        svm = self.svc_vm_mgr()
        hosting_entities = []
        with context.session.begin(subtransactions=True):
            # These resources are owned by the L3AdminTenant
            birth_date = timeutils.utcnow()
            for i in xrange(0, num):
                #TODO(bobmel): Make service VM resources creation plugin aware
                mgmt_port, t1_n, t1_sub, t1_p, t2_n, t2_sub, t2_p = (
                    svm.create_service_vm_resources(
                        self._plugin,
                        self.mgmt_nw_id(),
                        self.csr_mgmt_sec_grp_id(),
                        self.l3_tenant_id(),
                        cfg.CONF.max_routers_per_csr1kv,
                        mgmt_p_p_id=self.n1kv_mgmt_pp_id(),
                        t1_p_p_id=self.n1kv_t1_pp_id(),
                        t2_p_p_id=self.n1kv_t2_pp_id(),
                        t1_n_p_id=self.n1kv_t1_np_id(),
                        t2_n_p_id=self.n1kv_t2_np_id()))
                if mgmt_port is None:
                    # Required ports could not be created
                    return hosting_entities
                #Zip and flatten the two port list together
                ports = [x for t in zip(t1_p, t2_p) for x in t]
                host_ent = svm.dispatch_service_vm(cfg.CONF.csr1kv_image,
                                                   cfg.CONF.csr1kv_flavor,
                                                   mgmt_port,
                                                   ports)
                if host_ent is not None:
                    hosting_entities.append(host_ent)
                    he_db = HostingEntity(
                        id=host_ent['id'],
                        tenant_id=self.l3_tenant_id(),
                        admin_state_up=True,
                        host_type=cl3_const.CSR1KV_HOST,
#                        ip_address='10.0.100.5',
                        ip_address=mgmt_port['fixed_ips'][0]['ip_address'],
                        transport_port=cl3_const.CSR1kv_SSH_NETCONF_PORT,
                        l3_cfg_agent_id=None,
                        created_at=birth_date,
                        booting_time=cl3_const.CSR_BOOTING_TIME,
                        status=None,
                        tenant_bound=tenant_bound,
                        auto_delete_on_fail=True)
                    context.session.add(he_db)
                else:
                    # Fundamental error like could not contact Nova
                    # Cleanup anything we created
                    svm.cleanup_for_service_vm(self._plugin, mgmt_port,
                                               t1_n, t1_sub, t1_p,
                                               t2_n, t2_sub, t2_p)
                    return hosting_entities
        return hosting_entities

    #TODO(bob-melander): Refactor - to new resource management class
    def delete_unused_service_vm_hosting_entities(
            self, context, num, host_type=cl3_const.CSR1KV_HOST,
            tenant_bound=None):
        """Deletes <num> or less unused service VM instances that act as
        <host_type> hosting entities (for a certain tenant or for shared
        use). The number of deleted service vm instances is returned.
        """
        # Delete the "youngest" hosting entities since they are
        # more likely to not have finished booting
        query = context.session.query(HostingEntity)
        query = query.outerjoin(
            RouterHostingEntityBinding,
            HostingEntity.id == RouterHostingEntityBinding.hosting_entity_id)
        query = query.filter(and_(HostingEntity.host_type == host_type,
                                  HostingEntity.admin_state_up == True,
                                  HostingEntity.tenant_bound == None))
        query = query.group_by(HostingEntity.id)
        query = query.having(
            func.count(RouterHostingEntityBinding.router_id) == 0)
        query = query.order_by(
            HostingEntity.created_at.desc(),
            func.count(RouterHostingEntityBinding.router_id))
        he_candidates = query.all()
        svm = self.svc_vm_mgr()
        num_deleted = 0
        num_possible_to_delete = min(len(he_candidates), num)
        with context.session.begin(subtransactions=True):
            for i in xrange(0, num_possible_to_delete):
                if svm.delete_service_vm(he_candidates[i]['id'],
                                         self.mgmt_nw_id()):
                    context.session.delete(he_candidates[i])
                    num_deleted += 1
        return num_deleted

    #TODO(bob-melander): Refactor - to new resource management class
    def delete_dead_service_vm_hosting_entity(self, context, hosting_entity_db,
                                              logical_resource_ids):
        """ Deletes a presumably dead service VM. This will indirectly
        make all of its hosted resources (i.e., routers) unscheduled."""
        if hosting_entity_db is None:
            return
        if hosting_entity_db['host_type'] == cl3_const.CSR1KV_HOST:
            if self.hosting_scheduler._avail_svc_vm_slots < 0:
                self.hosting_scheduler.sync_service_vm_pool_counters(context)
            if hosting_entity_db['tenant_bound'] is not None:
                # The slots of a tenant bound hosting entity have already
                # been subtracted from available slots so no need to subtract
                # again when that dead hosting entity is deleted.
                reduce_by = 0
            else:
                # For an unbound (dead) hosting entity all its slots (used
                # and unused) should be counted when reducing the number of
                # available slots.
                reduce_by = cfg.CONF.max_routers_per_csr1kv
            if not self.svc_vm_mgr().delete_service_vm(hosting_entity_db['id'],
                                                       self.mgmt_nw_id()):
                LOG.error(_('Failed to delete hosting entity %s service VM. '
                            'Will un-register it anyway.'),
                          hosting_entity_db['id'])
            with context.session.begin(subtransactions=True):
                context.session.delete(hosting_entity_db)
            self.hosting_scheduler._avail_svc_vm_slots = (
                max(0, self.hosting_scheduler._avail_svc_vm_slots - reduce_by))
            self.hosting_scheduler.maintain_service_vm_pool(self, context)

    #TODO(bob-melander): Refactor - to new resource management class
    def delete_all_service_vm_hosting_entities(
            self, context, host_type=cl3_const.CSR1KV_HOST):
        query = context.session.query(HostingEntity)
        query = query.filter(HostingEntity.host_type == host_type)
        svm = self.svc_vm_mgr()
        for he in query:
            svm.delete_service_vm(he.id, self.mgmt_nw_id())
        # Adjust pool counters, easiest is to just re-sync.
        self.hosting_scheduler.sync_service_vm_pool_counters(context,
                                                             host_type)

    def _get_router_binding_info(self, context, id, load_he_info=True):
        query = context.session.query(RouterHostingEntityBinding)
        if load_he_info:
            query = query.options(joinedload('hosting_entity'))
        query = query.filter(RouterHostingEntityBinding.router_id == id)
        try:
            r_he_b = query.one()
            return r_he_b
        except exc.NoResultFound:
            # This should not happen
            LOG.error(_('DB inconsistency: No type and hosting info associated'
                        ' with router %s'), id)
            raise RouterBindingInfoError(router_id=id)
        except exc.MultipleResultsFound:
            # This should not happen either
            LOG.error(_('DB inconsistency: Multiple type and hosting info'
                        ' associated with router %s'), id)
            raise RouterBindingInfoError(router_id=id)

    def get_hosting_entities(self, context, hosting_entity_ids):
        query = context.session.query(HostingEntity)
        if len(hosting_entity_ids) > 1:
            query = query.options(joinedload('l3_cfg_agent')).filter(
                HostingEntity.id.in_(hosting_entity_ids))
        else:
            query = query.options(joinedload('l3_cfg_agent')).filter(
                HostingEntity.id == hosting_entity_ids[0])
        return query.all()

    def host_router(self, context, router_id):
        """Schedules non-hosted auto-schedulable router(s) on hosting entities.
        If <router_id> is given, then only the router with that id is
        scheduled (if it is non-hosted). If no <router_id> is given,
        then all non-hosted routers are scheduled.
        """
        if self.hosting_scheduler is None:
            return
        query = context.session.query(RouterHostingEntityBinding)
        query = query.filter(
            RouterHostingEntityBinding.router_type !=
            cl3_const.NAMESPACE_ROUTER_TYPE,
            RouterHostingEntityBinding.auto_schedule == True,
            RouterHostingEntityBinding.hosting_entity == None)
        if router_id:
            query = query.filter(
                RouterHostingEntityBinding.router_id == router_id)
        for r_he_binding in query:
            router = self._make_router_dict(r_he_binding.router)
            router['router_type'] = r_he_binding['router_type']
            router['share_host'] = r_he_binding['share_hosting_entity']
            self.hosting_scheduler.schedule_router_on_hosting_entity(
                self, context, router, r_he_binding)

    def _get_hosting_entity_bindings(self, context, id, load_routers=False,
                                    load_hosting_entity=False):
        query = context.session.query(RouterHostingEntityBinding)
        if load_routers:
            query = query.options(joinedload('router'))
        if load_hosting_entity:
            query = query.options(joinedload('hosting_entity'))
        query = query.filter(
            RouterHostingEntityBinding.hosting_entity_id == id)
        return query.all()

    #TODO(bob-melander): Consider refactor - to new resource management class
    def _process_non_responsive_hosting_entity(self, context,
                                               hosting_entity_db,
                                               logical_resource_ids):
        """ Host type specific processing of non responsive hosting entities.

        :param hosting_entity_db: db object for hosting entity
        :param logical_resource_ids: dict{'routers': [id1, id2, ...]}
        :return: True if hosting_entity has been deleted, otherwise False
        """
        if hosting_entity_db['host_type'] == cl3_const.CSR1KV_HOST:
            self.delete_dead_service_vm_hosting_entity(
                context, hosting_entity_db, logical_resource_ids)
            return True
        return False

    def handle_non_responding_hosting_entities(self, context, cfg_agent,
                                               hosting_entity_ids):
        hosting_entities = self.get_hosting_entities(context,
                                                     hosting_entity_ids)
        # Information to send to l3 cfg agent:
        #    {'he_id1': {'routers': [id1, id2, ...]},
        #     'he_id2': {'routers': [id3, id4, ...]}, ...}
        hosting_info = {}
        to_reschedule = []
        for he in hosting_entities:
            he_id = he['id']
            he_bindings = self._get_hosting_entity_bindings(context, he_id)
            router_ids = []
            for binding in he_bindings:
                router_ids.append(binding['router_id'])
                if binding['auto_schedule']:
                    to_reschedule.append(binding['router_id'])
            logical_resource_ids = {'routers': router_ids}
            was_deleted = self._process_non_responsive_hosting_entity(
                context, he, logical_resource_ids)
            if was_deleted:
                hosting_info[he_id] = logical_resource_ids
        l3_rpc_joint_agent_api.L3JointAgentNotify.hosting_entity_removed(
            context, hosting_info, False, cfg_agent)
        if to_reschedule:
            # Reschedule routers that should be auto-scheduled
            routers = self.get_sync_data_ext(context, to_reschedule)
            l3_rpc_joint_agent_api.L3JointAgentNotify.routers_updated(context,
                                                                      routers)

    # Make parent's call to get_sync_data(...) a noop
    def get_sync_data(self, context, router_ids=None, active=None):
        return []

    def get_sync_data_ext(self, context, router_ids=None, active=None,
                          ext_gw_change_status=None,
                          int_if_change_status=None):
        """Query routers and their related floating_ips, interfaces.
        Adds information about hosting entity as well as trunking.
        """
        sync_data = super(L3_router_appliance_db_mixin, self).get_sync_data(
            context, router_ids, active)
        for r in sync_data:
            self._add_type_and_hosting_info(context, r)
            host_type = (r.get('hosting_entity') or {}).get('host_type', '')
            #TODO(bobmel): Vif management needs further modularization
            #TODO(bobmel): to support non-trunk case etc.
            self._populate_ha_information(context, r)
            if self._plugin in [cl3_const.N1KV_PLUGIN, cl3_const.OVS_PLUGIN]:
                if host_type in [cl3_const.NEXUS_AGGR_HOST,
                                 cl3_const.NEXUS_TOR_HOST]:
                    self._populate_vlan_trunk_info(
                        context, r, ext_gw_change_status=ext_gw_change_status,
                        int_if_change_status=int_if_change_status)
                elif host_type == cl3_const.CSR1KV_HOST:
                    self._populate_port_trunk_info(
                        context, r, ext_gw_change_status=ext_gw_change_status,
                        int_if_change_status=int_if_change_status)
        return sync_data

    def _add_type_and_hosting_info(self, context, router, binding_info=None,
                                   schedule=True):
        """Adds type and hosting entity information to a router."""
        try:
            if binding_info is None:
                binding_info = self._get_router_binding_info(context,
                                                             router['id'])
        except RouterBindingInfoError:
            return
        router['router_type'] = binding_info['router_type']
        router['share_host'] = binding_info['share_hosting_entity']
        if binding_info.router_type == cl3_const.NAMESPACE_ROUTER_TYPE:
            return
        if binding_info.hosting_entity is None and schedule:
            # This router has not been scheduled to a hosting entity
            # so we try to do it now.
            self.hosting_scheduler.schedule_router_on_hosting_entity(
                self, context, router, binding_info)
        if binding_info.hosting_entity is None:
            router['hosting_entity'] = None
        else:
            router['hosting_entity'] = {
                'id': binding_info.hosting_entity.id,
                'host_type': binding_info.hosting_entity.host_type,
                'ip_address': binding_info.hosting_entity.ip_address,
                'port': binding_info.hosting_entity.transport_port,
                'created_at': str(binding_info.hosting_entity.created_at),
                'booting_time': binding_info.hosting_entity.booting_time,
                #TODO(bob-melander: fetch these from credentials db
                'user_name': cfg.CONF.csr1kv_user_name,
                'password': cfg.CONF.csr1kv_password}

    def _get_hosting_port_name(self, context, net_id):
        if self._plugin == cl3_const.N1KV_PLUGIN:
            net_data = self.get_network(context, net_id, [pr_net.NETWORK_TYPE])
            if net_data.get(pr_net.NETWORK_TYPE) == 'vlan':
                return cl3_const.T2_PORT_NAME
            else:
                return cl3_const.T1_PORT_NAME
        return cl3_const.T1_PORT_NAME

    def _get_network_type(self, hosting_port_name):
        if hosting_port_name == cl3_const.T2_PORT_NAME:
            return 'vlan'
        else:
            return 'vxlan'

    def _populate_vlan_trunk_info(self, context, router,
                                  ext_gw_change_status=None,
                                  int_if_change_status=None):

        for itfc in (router.get(l3_constants.INTERFACE_KEY, []) +
                     (router.get('gw_port') and [router.get('gw_port')]
                      or [])):
            port_db = self._get_port(context, itfc['id'])
            tr_info = port_db.trunk_info
            if tr_info is None:
                tags = self.get_networks(context, {'id': [itfc['network_id']]},
                                         [pr_net.SEGMENTATION_ID])
                used_vlan = (None if tags == [] else
                             tags[0].get(pr_net.SEGMENTATION_ID))
                with context.session.begin(subtransactions=True):
                    tr_info = TrunkInfo(
                        router_port_id=port_db['id'],
                        network_type='VLAN',
                        hosting_port_id=port_db['id'],
                        segmentation_tag=used_vlan)
                    context.session.add(tr_info)
            itfc['trunk_info'] = {
                'hosting_port_id': tr_info.hosting_port_id,
                'hosting_mac': itfc['mac_address'],
                'hosting_port_name': itfc['name'],
                'segmentation_id': tr_info.segmentation_tag}

    def _populate_port_trunk_info(self, context, router,
                                  ext_gw_change_status=None,
                                  int_if_change_status=None):
        """Populate router ports with trunking information.

        This function should only be called for routers that are hosted
        by hosting entities that use VLANs, e.g., service VMs like CSR1kv.
        """

        # We only populate trunk info, i.e., reach here, if the
        # router has been scheduled to a hosting entity. Hence this
        # a good place to allocate hosting ports to the router ports.
        if ext_gw_change_status is None:
            ext_gw_change_status = {}
        if int_if_change_status is None:
            int_if_change_status = {}
        trunk_data = {
            cl3_const.T1_PORT_NAME: {
                'update': False, 'network_id': None,'mappings': {}},
            cl3_const.T2_PORT_NAME: {
                'update': False, 'network_id': None, 'mappings': {}}}
        tr_info = None
        hosting_pdata = {cl3_const.T1_PORT_NAME: {'mac': None, 'name': None},
                         cl3_const.T2_PORT_NAME: {'mac': None, 'name': None}}
        if router['external_gateway_info'] is None:
            trunk_port_name = None
        else:
            trunk_port_name = self._get_hosting_port_name(
                context, router['gw_port']['network_id'])
            tr_info, did_allocation = self._populate_trunk_for_port(
                context, router['gw_port'], router['hosting_entity']['id'],
                router['id'], trunk_port_name, hosting_pdata[trunk_port_name])
        if ext_gw_change_status.get('changed', False):
            old_p_t_type = ext_gw_change_status['old_hosting_port_name']
            if (ext_gw_change_status['old_trunk_nw_id'] is not None and
                    trunk_port_name != old_p_t_type):
                # Gateway network has been removed or has changed type
                # (vlan -> vxlan or vice versa) so we must ensure that
                # the the old trunk is removed.
                trunk_data[old_p_t_type]['update'] = True
                trunk_data[old_p_t_type]['port_id'] = (
                    ext_gw_change_status['old_hosting_port_id'])
                trunk_data[old_p_t_type]['network_id'] = (
                    ext_gw_change_status['old_trunk_nw_id'])
            if tr_info is not None:
                trunk_data[trunk_port_name]['mappings'] = (
                    {router['gw_port']['network_id']:
                        tr_info.segmentation_tag})
                trunk_data[trunk_port_name]['port_id'] = (
                    tr_info.hosting_port_id)
                trunk_data[trunk_port_name]['network_id'] = (
                    tr_info.hosting_port.network_id)
                if trunk_port_name == cl3_const.T1_PORT_NAME:
                    # The vxlan trunk must be updated separately
                    # since vlan trunk is updated during allocation.
                    trunk_data[trunk_port_name]['update'] = True
        if (int_if_change_status.get('changed', False) and
            int_if_change_status.get('old_trunk_nw_id') is not None):
            # An internal network has been removed, and it may be the last
            # one, so we must ensure trunking of that network is cancelled.
            old_p_t_type = int_if_change_status['old_hosting_port_name']
            trunk_data[old_p_t_type]['update'] = True
            trunk_data[old_p_t_type]['port_id'] = (
                int_if_change_status['old_hosting_port_id'])
            trunk_data[old_p_t_type]['network_id'] = (
                int_if_change_status['old_trunk_nw_id'])
        for itfc in router.get(l3_constants.INTERFACE_KEY, []):
            trunk_port_name = self._get_hosting_port_name(context,
                                                          itfc['network_id'])
            tr_info, did_allocation = self._populate_trunk_for_port(
                context, itfc, router['hosting_entity']['id'],
                router['id'], trunk_port_name, hosting_pdata[trunk_port_name])
            if tr_info is not None:
                trunk_data[trunk_port_name]['port_id'] = (
                    tr_info.hosting_port_id)
                trunk_data[trunk_port_name]['network_id'] = (
                    tr_info.hosting_port.network_id)
                if trunk_port_name == cl3_const.T1_PORT_NAME:
                    # The vxlan trunk must be updated separately
                    # since vlan trunk is updated during allocation.
                    trunk_data[trunk_port_name]['update'] = True
            trunk_data[trunk_port_name]['update'] |= did_allocation
            if (hosting_pdata[trunk_port_name]['mac'] is None and
                    tr_info is not None):
                hosting_pdata[trunk_port_name] = {
                    'mac': itfc['trunk_info']['hosting_mac'],
                    'name': itfc['trunk_info']['hosting_port_name']}
        for td in trunk_data.values():
            if td['update']:
                self._extend_trunk_mapping(
                    context, router['id'], device_owner=None,
                    hosting_port_id=td['port_id'],
                    mapping=td['mappings'])
                self._update_trunking_on_hosting_port(context,
                                                      td['network_id'],
                                                      td['mappings'])

    def _populate_trunk_for_port(self, context, port, hosting_entity_id,
                                 router_id, trunk_port_name, hosting_pdata):
        port_db = self._get_port(context, port['id'])
        tr_info = port_db.trunk_info
        new_allocation = False
        if tr_info is None:
            # The port does not yet have a hosting port so
            # allocate one now
            tr_info = self._allocate_hosting_port(
                context, port_db, hosting_entity_id, router_id,
                trunk_port_name)
            if tr_info is None:
                # This should not happen but just in case ...
                LOG.error(_('Failed to allocate hosting port '
                            'for port %s'), port['id'])
                port['trunk_info'] = None
                return None, new_allocation
            else:
                new_allocation = True
        if hosting_pdata.get('mac') is None:
            p_data = self.get_port(context, tr_info.hosting_port_id,
                                   ['mac_address', 'name'])
            hosting_pdata['mac'] = p_data['mac_address']
            hosting_pdata['name'] = p_data['name']
        # Including MAC address of hosting port so L3CfgAgent can easily
        # determine which VM VIF to configure VLAN sub-interface on.
        port['trunk_info'] = {'hosting_port_id': tr_info.hosting_port_id,
                              'hosting_mac': hosting_pdata.get('mac'),
                              'hosting_port_name': hosting_pdata.get('name'),
                              'segmentation_id': tr_info.segmentation_tag}
        return tr_info, new_allocation

    def _allocate_hosting_port(self, context, port_db, hosting_entity_id,
                               router_id, trunk_port_name):
        net_type=self._get_network_type(trunk_port_name)
        allocations = self._get_router_ports_with_trunkinfo_qry(
            context, router_id).all()
        trunk_mappings = {}
        if len(allocations) == 0:
            # Router has no ports with hosting port allocated to them yet
            # whatsoever, so we select an unused port (that trunks networks
            # of correct type) on the hosting entity.
            id_allocated_port = self._get_unused_service_vm_trunk_port(
                context, hosting_entity_id, trunk_port_name)
        else:
            # Router has at least one port with hosting port allocated to it.
            # If there is only one allocated hosting port then it may be for
            # the wrong network type. Iterate to determine the hosting port.
            id_allocated_port = None
            for item in allocations:
                if item.trunk_info['network_type'] == net_type:
                    # For VXLAN we need to determine used link local tags.
                    # For VLAN we don't need to but the following lines will
                    # be performed once anyway since we break out of the
                    # loop later. That does not matter.
                    tag = item.trunk_info['segmentation_tag']
                    trunk_mappings[item['network_id']] = tag
                    id_allocated_port = item.trunk_info['hosting_port_id']
                else:
                    port_twin_id = item.trunk_info['hosting_port_id']
                if trunk_port_name == cl3_const.T2_PORT_NAME:
                    # For a router port belonging to a VLAN network we can
                    # break here since we now know (or have information to
                    # determine) hosting_port and the VLAN tag is provided by
                    # the core plugin.
                    break
            if id_allocated_port is None:
                # Router only had hosting port for wrong network
                # type allocated yet. So get that port's sibling.
                id_allocated_port = self._get_other_port_id_in_pair(
                    context, port_twin_id, hosting_entity_id)
        if id_allocated_port is None:
            # Database must have been messed up if this happens ...
            return
        if trunk_port_name == cl3_const.T1_PORT_NAME:
            # For VLXAN we choose the (link local) VLAN tag
            used_tags = set(trunk_mappings.values())
            allocated_vlan = min(sorted(FULL_VLAN_SET - used_tags))
        else:
            # For VLAN core plugin provides VLAN tag.
            trunk_mappings[port_db['network_id']] = None

            tags = self.get_networks(context, {'id': [port_db['network_id']]},
                                     [pr_net.SEGMENTATION_ID])
            allocated_vlan = (None if tags == []
                              else tags[0].get(pr_net.SEGMENTATION_ID))
        if allocated_vlan is None:
            # Database must have been messed up if this happens ...
            return
        with context.session.begin(subtransactions=True):
            tr_info = TrunkInfo(
                router_port_id=port_db['id'],
                network_type=net_type,
                hosting_port_id=id_allocated_port,
                segmentation_tag=allocated_vlan)
            context.session.add(tr_info)
            context.session.expire(port_db)
        return tr_info

    def _get_router_port_db_on_subnet(self, context, router_id, subnet):
        try:
            rport_qry = context.session.query(models_v2.Port)
            ports = rport_qry.filter_by(
                device_id=router_id,
                device_owner=l3_db.DEVICE_OWNER_ROUTER_INTF,
                network_id=subnet['network_id'])
            for p in ports:
                if p['fixed_ips'][0]['subnet_id'] == subnet['id']:
                    return p
        except exc.NoResultFound:
            return

    def _get_router_ports_with_trunkinfo_qry(self, context, router_id,
                                             device_owner=None,
                                             hosting_port_id=None):
        # Query for a router's ports that have trunking information
        query = context.session.query(models_v2.Port)
        query = query.join(TrunkInfo,
                           models_v2.Port.id == TrunkInfo.router_port_id)
        query = query.filter(models_v2.Port.device_id == router_id)
        if device_owner is not None:
            query = query.filter(models_v2.Port.device_owner == device_owner)
        if hosting_port_id is not None:
            query = query.filter(
                TrunkInfo.hosting_port_id == hosting_port_id)
        return query

    def _update_trunking_on_hosting_port(self, context, trunk_network_id,
                                         trunk_mappings):
        if self._plugin == cl3_const.N1KV_PLUGIN:
            np_id_t_nw = self.get_network(context, trunk_network_id,
                                          [n1kv_profile.PROFILE_ID,
                                           n1kv_profile.MEMBER_SEGMENTS])
            if np_id_t_nw.get(n1kv_profile.PROFILE_ID) == self.n1kv_t1_np_id():
                # for vxlan trunked segment, id:s end with ':'link local vlan tag
                nets_to_trunk = set([k + ':' + str(v)
                                     for k, v in trunk_mappings.items()])
            else:
                # not so for vlan trunked segments
                nets_to_trunk = trunk_mappings.keys()
                n_id_tags = self.get_networks(
                    context, {'id': nets_to_trunk},
                    ['id', pr_net.SEGMENTATION_ID])
                nets_to_trunk = set(nets_to_trunk)
                # fill in actual VLAN tags that are used
                for info in n_id_tags:
                    trunk_mappings[info['id']] = info[pr_net.SEGMENTATION_ID]
            curr_trunks = set(np_id_t_nw[n1kv_profile.MEMBER_SEGMENTS])
            trunks_to_add = nets_to_trunk - curr_trunks
            trunks_to_del = curr_trunks - nets_to_trunk
            network_dict = {}
            if len(trunks_to_add) > 0:
                network_dict.update(
                    {n1kv_profile.SEGMENT_ADD:
                     ', '.join(str(ta) for ta in trunks_to_add)})
            if len(trunks_to_del) > 0:
                network_dict.update(
                    {n1kv_profile.SEGMENT_DEL:
                     ', '.join(str(td) for td in trunks_to_del)})
            if len(network_dict) > 0:
                LOG.info(_('Updating trunking'))
                #TODO(bobmel): enable below line when N1kv works.
                #self.update_network(context, trunk_network_id,
                #                    {'network': network_dict})
            return trunk_mappings
        else:
            network_dict = {'network': {TRUNKED_NETWORKS: trunk_mappings}}
            net = self.update_network(context, trunk_network_id, network_dict)
            return net.get(TRUNKED_NETWORKS)

    def _get_trunk_port_and_network_ids(self, context, port_db):
        if port_db and port_db.trunk_info and port_db.trunk_info.hosting_port:
            return (port_db.trunk_info.hosting_port['id'],
                    port_db.trunk_info.hosting_port['network_id'])
        else:
            return None, None

    def _extend_trunk_mapping(self, context, router_id, device_owner,
                              hosting_port_id, mapping):
        query = self._get_router_ports_with_trunkinfo_qry(
            context, router_id, device_owner, hosting_port_id)
        for port in query:
            mapping[port['network_id']] = port.trunk_info.segmentation_tag

    def _get_unused_service_vm_trunk_port(self, context, he_id, name):
        # mysql> SELECT * FROM ports WHERE device_id = 'he_id1' AND
        # id NOT IN (SELECT hosting_port_id FROM trunkinfos) AND
        # name LIKE '%t1%'
        # ORDER BY name;
        attempts = 0
        while True:
            stmt = context.session.query(TrunkInfo.hosting_port_id).subquery()
            query = context.session.query(models_v2.Port.id)
            query = query.filter(and_(models_v2.Port.device_id == he_id,
                                      ~models_v2.Port.id.in_(stmt),
                                      models_v2.Port.name.like('%' + name +
                                                               '%')))
            query = query.order_by(models_v2.Port.name)
            res = query.first()
            if res is None:
                if attempts >= MAX_HOSTING_PORT_LOOKUP_ATTEMPTS:
                    # This should not happen ...
                    LOG.error(_('Trunk port DB inconsistency for '
                                'hosting entity %s'), he_id)
                    return
                else:
                    # The service VM may not have plugged its VIF into the
                    # Neutron Port yet so we wait and make another lookup.
                    attempts += 1
                    LOG.info(_('Attempt %(attempt)d to find trunk ports for '
                               'hosting entity %(he_id)s failed. Trying '
                               'again in %(time)d seconds.'),
                             {'attempt': attempts, 'he_id': he_id,
                              'time': SECONDS_BETWEEN_HOSTING_PORT_LOOKSUPS})
                    eventlet.sleep(SECONDS_BETWEEN_HOSTING_PORT_LOOKSUPS)
            else:
                break
        return res[0]

    def _get_other_port_id_in_pair(self, context, port_id, hosting_entity_id):
        query = context.session.query(models_v2.Port)
        query = query.filter(models_v2.Port.id == port_id)
        try:
            port = query.one()
            name, index = port['name'].split(':')
            name += ':'
            if name == cl3_const.T1_PORT_NAME:
                other_port_name = cl3_const.T2_PORT_NAME
            else:
                other_port_name = cl3_const.T1_PORT_NAME
            query = context.session.query(models_v2.Port)
            query = query.filter(models_v2.Port.device_id == hosting_entity_id,
                                 models_v2.Port.name ==
                                 other_port_name + index)
            other_port = query.one()
            return other_port['id']
        except (exc.NoResultFound, exc.MultipleResultsFound):
            # This should not happen ...
            LOG.error(_('Port trunk pair DB inconsistency for port %s'),
                      port_id)
            return
