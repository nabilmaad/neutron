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
# @author: Hareesh Puthalath, Cisco Systems, Inc.
# @author: Bob Melander, Cisco Systems, Inc.

import os

from oslo.config import cfg as q_conf

from novaclient.v1_1 import client
from novaclient import exceptions as n_exc
from novaclient import utils as n_utils
from quantum.api.v2 import attributes
from quantum.common import exceptions as q_exc
from quantum import context as q_context
from quantum import manager
from quantum.openstack.common import uuidutils
from quantum.openstack.common import log as logging
from quantum.plugins.cisco.l3.common import constants

import netaddr

LOG = logging.getLogger(__name__)


# TODO(bob-melander): This should be used as a driver and fake version should
# be created for unit tests.
class ServiceVMManager:

    def __init__(self, user=None, passwd=None, l3_admin_tenant=None,
                 auth_url=None):
        self._nclient = client.Client(user, passwd, l3_admin_tenant, auth_url,
                                     service_type="compute")
        self._context = q_context.get_admin_context()
        #self._context.tenant_id=tenant_id
        self._core_plugin = manager.QuantumManager.get_plugin()
        #self.csr_config_template = "./csr_cfgs/cfg_template"

    def get_service_vm_status(self, vm_id):
        try:
            status = self._nclient.servers.get(vm_id).status
        except (n_exc.UnsupportedVersion, n_exc.CommandError,
                n_exc.AuthorizationFailure, n_exc.NoUniqueMatch,
                n_exc.AuthSystemNotFound, n_exc.NoTokenLookupException,
                n_exc.EndpointNotFound, n_exc.AmbiguousEndpoints,
                n_exc.ConnectionRefused, n_exc.ClientException) as e:
            LOG.error(_('Failed to get status of service VM instance %(id)s, '
                        'due to %(err)s'), {'id': vm_id, 'err': e})
            status = constants.SVM_ERROR
        return status

    def dispatch_service_vm(self, vm_image, vm_flavor, mgmt_port,
                            ports=None):
        nics = [{'port-id': mgmt_port['id']}]

        for port in ports:
            nics.append({'port-id': port['id']})

        try:
            cfg = self.generate_config_for_csr(mgmt_port)
            cfg_f = open(cfg)
            files = {'iosxe_config.txt': cfg_f}
        except IOError:
            return None

        try:
            image = n_utils.find_resource(self._nclient.images, vm_image)
            flavor = n_utils.find_resource(self._nclient.flavors, vm_flavor)
        except n_exc.CommandError as e:
            LOG.error(_('Failure: %s'), e)
            return None
        try:
            server = self._nclient.servers.create('csr1kv_nrouter', image.id,
                                                  flavor.id, nics=nics,
                                                  files=files,
                                                  config_drive=True)
        except (n_exc.UnsupportedVersion, n_exc.CommandError,
                n_exc.AuthorizationFailure, n_exc.NoUniqueMatch,
                n_exc.AuthSystemNotFound, n_exc.NoTokenLookupException,
                n_exc.EndpointNotFound, n_exc.AmbiguousEndpoints,
                n_exc.ConnectionRefused, n_exc.ClientException) as e:
            LOG.error(_('Failed to create service VM instance: %s'), e)
            return None
        res = {'id': server.id}
        return res

    def delete_service_vm(self, vm_id, mgmt_nw_id, delete_networks=True):
        to_delete = []
        ports = self._core_plugin.get_ports(self._context,
                                            filters={'device_id': [vm_id]})

        result = True
        for port in ports:
            if port['network_id'] != mgmt_nw_id:
                if delete_networks:
                    to_delete.append({'subnet': port['fixed_ips'][0]['subnet_id'],
                                      'net': port['network_id']})
            else:
                # id of mgmt port is used in config file name so
                # we delete that file here.
                self.delete_config_file_for_csr(port['id'])
            # We delete the ports here even though Nova will try to do the same
            # later. But Nova's delete comes to late since we want to delete the
            # t1 and t2 subnets and networks in this function.
            try:
                self._core_plugin.delete_port(self._context, port['id'])
            except q_exc.QuantumException as e:
                LOG.error(_('Failed to delete port %(port_id)s for '
                            'service VM %(id)s due to %(err)s'),
                          {'port_id': port['id'], 'id': vm_id, 'err': e})
                result = False
        try:
            self._nclient.servers.delete(vm_id)
        except (n_exc.UnsupportedVersion, n_exc.CommandError,
                n_exc.AuthorizationFailure, n_exc.NoUniqueMatch,
                n_exc.AuthSystemNotFound, n_exc.NoTokenLookupException,
                n_exc.EndpointNotFound, n_exc.AmbiguousEndpoints,
                n_exc.ConnectionRefused, n_exc.ClientException) as e:
            LOG.error(_('Failed to delete service VM instance %(id)s, '
                        'due to %(err)s'), {'id': vm_id, 'err': e})
            result = False
        for ids in to_delete:
            try:
                # N1kv plugin requires that subnet is deleted first
                self._core_plugin.delete_subnet(self._context, ids['subnet'])
            except q_exc.QuantumException as e:
                LOG.error(_('Failed to delete subnet %(subnet_id)s for '
                            'service VM %(id)s due to %(err)s'),
                          {'subnet_id': ids['subnet'], 'id': vm_id, 'err': e})
            try:
                self._core_plugin.delete_network(self._context, ids['net'])
            except q_exc.QuantumException as e:
                LOG.error(_('Failed to delete network %(net_id)s for service '
                            'VM %(id) due to %(err)s'),
                          {'net_id': ids['net'], 'id': vm_id, 'err': e})
        return result

    def cleanup_for_service_vm(self, plugin, mgmt_port, t1_n, t1_sub, t1_p,
                               t2_n, t2_sub, t2_p):
        if plugin == constants.N1KV_PLUGIN:
            self.cleanup_for_service_vm_n1kv(mgmt_port,
                                             t1_n, t1_sub, t1_p,
                                             t2_n, t2_sub, t2_p)
        else:
            self.cleanup_for_service_vm_ovs(mgmt_port, t1_n, t1_sub, t1_p,
                                            t2_n, t2_sub, t2_p)

    def cleanup_for_service_vm_n1kv(self, mgmt_port, t1_n, t1_sub, t1_p,
                                    t2_n, t2_sub, t2_p):
         # Remove anything created.
        if mgmt_port is not None:
            try:
                self._core_plugin.delete_port(self._context, mgmt_port['id'])
            except q_exc.QuantumException as e:
                LOG.error(_('Failed to delete management port %(port_id)s for '
                            'service vm due to %(err)s'),
                          {'port_id': mgmt_port['id'], 'err': e})
        for item in t1_p + t2_p:
            try:
                self._core_plugin.delete_port(self._context, item['id'])
            except q_exc.QuantumException as e:
                LOG.error(_('Failed to delete trunk port %(port_id)s for '
                            'service vm due to %(err)s'),
                          {'port_id': item['id'], 'err': e})
        for item in t1_sub + t2_sub:
            try:
                self._core_plugin.delete_subnet(self._context, item['id'])
            except q_exc.QuantumException as e:
                LOG.error(_('Failed to delete subnet %(subnet_id)s for '
                            'service vm due to %(err)s'),
                          {'subnet_id': item['id'], 'err': e})
        for item in t1_n + t2_n:
            try:
                self._core_plugin.delete_network(self._context, item['id'])
            except q_exc.QuantumException as e:
                LOG.error(_('Failed to delete trunk network %(net_id)s for '
                            'service vm due to %(err)s'),
                          {'net_id': item['id'], 'err': e})

    def cleanup_for_service_vm_ovs(self, mgmt_port, t1_n, t1_sub, t1_p,
                                   t2_n, t2_sub, t2_p):
         # Remove anything created.
        if mgmt_port is not None:
            try:
                self._core_plugin.delete_port(self._context, mgmt_port['id'])
            except q_exc.QuantumException as e:
                LOG.error(_('Failed to delete management port %(port_id)s for '
                            'service vm due to %(err)s'),
                          {'port_id': mgmt_port['id'], 'err': e})
        for item in t1_p + t2_p:
            try:
                self._core_plugin.delete_port(self._context, item['id'])
            except q_exc.QuantumException as e:
                LOG.error(_('Failed to delete trunk port %(port_id)s for '
                            'service vm due to %(err)s'),
                          {'port_id': item['id'], 'err': e})
        for item in t1_sub + t2_sub:
            try:
                self._core_plugin.delete_subnet(self._context, item['id'])
            except q_exc.QuantumException as e:
                LOG.error(_('Failed to delete subnet %(subnet_id)s for '
                            'service vm due to %(err)s'),
                          {'subnet_id': item['id'], 'err': e})
        for item in t1_n + t2_n:
            try:
                self._core_plugin.delete_network(self._context, item['id'])
            except q_exc.QuantumException as e:
                LOG.error(_('Failed to delete trunk network %(net_id)s for '
                            'service vm due to %(err)s'),
                          {'net_id': item['id'], 'err': e})

    def create_service_vm_resources(self, plugin, mgmt_nw_id, mgmt_sec_grp_id,
                                    tenant_id, max_hosted, **kwargs):
        if plugin == constants.N1KV_PLUGIN:
            return self.create_service_vm_resources_n1kv(
                mgmt_nw_id, mgmt_sec_grp_id, tenant_id, max_hosted, **kwargs)
        else:
            return self.create_service_vm_resources_ovs(
                mgmt_nw_id, mgmt_sec_grp_id, tenant_id, max_hosted)

    def create_service_vm_resources_n1kv(self, mgmt_nw_id, mgmt_sec_grp_id,
                                         tenant_id, max_hosted, **kwargs):
        mgmt_port = None
        t1_n, t1_p, t2_n, t2_p = [], [], [], []
        t1_sub, t2_sub = [], []
        if mgmt_nw_id is not None and tenant_id is not None:
            # Create port for mgmt interface
            p_spec = {'port': {
                'tenant_id': tenant_id,
                'admin_state_up': True,
                'name': 'mgmt',
                'network_id': mgmt_nw_id,
                'mac_address': attributes.ATTR_NOT_SPECIFIED,
                'fixed_ips': attributes.ATTR_NOT_SPECIFIED,
                'n1kv:profile_id': kwargs.get('mgmt_p_p_id', {}).get('id'),
                'device_id': "",
                'device_owner': ""}}
            try:
                mgmt_port = self._core_plugin.create_port(self._context,
                                                          p_spec)
                # The trunk networks
                n_spec = {'network': {'tenant_id': tenant_id,
                                      'admin_state_up': True,
                                      'name': constants.T1_NETWORK_NAME,
                                      'shared': False}}
                # Until Nova allows spinning up VMs with VIFs on
                # networks without subnet(s) we create "dummy" subnets
                # for the trunk networks
                sub_spec = {'subnet': {
                    'tenant_id': tenant_id,
                    'admin_state_up': True,
                    'cidr': constants.SUB_PREFX,
                    'enable_dhcp': False,
                    'gateway_ip': attributes.ATTR_NOT_SPECIFIED,
                    'allocation_pools': attributes.ATTR_NOT_SPECIFIED,
                    'ip_version': 4,
                    'dns_nameservers': attributes.ATTR_NOT_SPECIFIED,
                    'host_routes': attributes.ATTR_NOT_SPECIFIED}}
                for i in xrange(0, max_hosted):
                    # Create T1 trunk network for this router
                    indx = str(i + 1)
                    n_spec['network'].update(
                        {'name': constants.T1_NETWORK_NAME + indx,
                         'n1kv:profile_id': kwargs.get('t1_n_p_id',
                                                       {}).get('id')})
                    t1_n.append(self._core_plugin.create_network(
                        self._context, n_spec))
                    LOG.debug(_('Created T1 network with name %(name)s and '
                                'id %(id)s'),
                              {'name': constants.T1_NETWORK_NAME + indx,
                               'id': t1_n[i]['id']})
                    # Create dummy subnet for this trunk network
                    sub_spec['subnet'].update(
                        {'name': constants.T1_SUBNET_NAME + indx,
                         'network_id': t1_n[i]['id']})
                    t1_sub.append(self._core_plugin.create_subnet(self._context,
                                                                  sub_spec))
                    # Create T1 port for this router
                    p_spec['port'].update(
                        {'name': constants.T1_PORT_NAME + indx,
                         'network_id': t1_n[i]['id'],
                         'n1kv:profile_id': kwargs.get('t1_p_p_id',
                                                       {}).get('id')})
                    t1_p.append(self._core_plugin.create_port(self._context,
                                                              p_spec))
                    LOG.debug(_('Created T1 port with name %(name)s,  '
                                'id %(id)s and subnet %(subnet)s'),
                              {'name': t1_n[i]['name'],
                               'id': t1_n[i]['id'],
                               'subnet': t1_sub[i]['id']})
                    # Create T2 trunk network for this router
                    n_spec['network'].update(
                        {'name': constants.T2_NETWORK_NAME + indx,
                         'n1kv:profile_id': kwargs.get('t2_n_p_id',
                                                       {}).get('id')})
                    t2_n.append(self._core_plugin.create_network(self._context,
                                                                 n_spec))
                    LOG.debug(_('Created T2 network with name %(name)s and '
                                'id %(id)s'),
                              {'name': constants.T2_NETWORK_NAME + indx,
                               'id': t2_n[i]['id']})
                    # Create dummy subnet for this trunk network
                    sub_spec['subnet'].update(
                        {'name': constants.T2_SUBNET_NAME + indx,
                         'network_id': t2_n[i]['id']})
                    t2_sub.append(self._core_plugin.create_subnet(self._context,
                                                                  sub_spec))
                    # Create T2 port for this router
                    p_spec['port'].update(
                        {'name': constants.T2_PORT_NAME + indx,
                         'network_id': t2_n[i]['id'],
                         'n1kv:profile_id': kwargs.get('t2_p_p_id',
                                                       {}).get('id')})
                    t2_p.append(self._core_plugin.create_port(self._context,
                                                              p_spec))
                    LOG.debug(_('Created T2 port with name %(name)s,  '
                                'id %(id)s and subnet %(subnet)s'),
                              {'name': t2_n[i]['name'],
                               'id': t2_n[i]['id'],
                               'subnet': t2_sub[i]['id']})
            except q_exc.QuantumException as e:
                LOG.error(_('Error %s when creating service VM resources. '
                            'Cleaning up.'), e)
                self.cleanup_for_service_vm_n1kv(mgmt_port, t1_n, t1_sub, t1_p,
                                                 t2_n, t2_sub, t2_p)
                mgmt_port = None
                t1_n, t1_p, t2_n, t2_p = [], [], [], []
        return mgmt_port, t1_n, t1_sub, t1_p, t2_n, t2_sub, t2_p

    def create_service_vm_resources_ovs(self, mgmt_nw_id, csr_mgmt_sec_grp_id,
                                        tenant_id, max_hosted):
        mgmt_port = None
        t1_n, t1_p, t2_n, t2_p = [], [], [], []
        t1_sub, t2_sub = [], []
        if mgmt_nw_id is not None and tenant_id is not None:
            # Create port for mgmt interface
            p_spec = {'port': {'tenant_id': tenant_id,
                               'admin_state_up': True,
                               'name': 'mgmt',
                               'network_id': mgmt_nw_id,
                               'mac_address': attributes.ATTR_NOT_SPECIFIED,
                               'fixed_ips': attributes.ATTR_NOT_SPECIFIED,
                               'security_groups': [csr_mgmt_sec_grp_id],
                               'device_id': "",
                               'device_owner': ""}}
            try:
                mgmt_port = self._core_plugin.create_port(self._context, p_spec)
                # No security groups on the trunk ports since
                # they have no IP address
                p_spec['port']['security_groups'] = []
                # The trunk networks
                n_spec = {'network': {'tenant_id': tenant_id,
                                      'admin_state_up': True,
                                      'name': constants.T1_NETWORK_NAME,
                                      'shared': False,
                                      'trunkport:trunked_networks': {}}}
                # Until Nova allows spinning up VMs with vifs on networks without
                # subnet(s) we create "dummy" subnets for the trunk networks
                sub_spec = {'subnet': {'tenant_id': tenant_id,
                                       'admin_state_up': True,
                                       'cidr': constants.SUB_PREFX,
                                       'enable_dhcp': False,
                                       'gateway_ip': attributes.ATTR_NOT_SPECIFIED,
                                       'allocation_pools': attributes.ATTR_NOT_SPECIFIED,
                                       'ip_version': 4,
                                       'dns_nameservers': attributes.ATTR_NOT_SPECIFIED,
                                       'host_routes': attributes.ATTR_NOT_SPECIFIED
                                       }
                            }
                for i in xrange(0, max_hosted):
                    # Create T1 trunk network for this router
                    indx = str(i + 1)
                    n_spec['network'].update(
                        {'name':constants.T1_NETWORK_NAME + indx})
                    t1_n.append(self._core_plugin.create_network(
                        self._context, n_spec))
                    LOG.debug(_('Created T1 network with name %(name)s and '
                                'id %(id)s'),
                              {'name': constants.T1_NETWORK_NAME + indx,
                               'id': t1_n[i]['id']})
                    sub_spec['subnet'].update(
                        {'name': constants.T1_SUBNET_NAME + indx,
                         'network_id': t1_n[i]['id']})
                    t1_sub.append(self._core_plugin.create_subnet(self._context,
                                                                  sub_spec))
                    # Create T1 port for this router
                    p_spec['port'].update(
                        {'name': constants.T1_PORT_NAME + indx,
                         'network_id': t1_n[i]['id'],
                         'fixed_ips': [{'subnet_id': t1_sub[i]['id']}]})
                    t1_p.append(self._core_plugin.create_port(self._context,
                                                              p_spec))
                    LOG.debug(_('Created T1 port with name %(name)s,  '
                                'id %(id)s and subnet %(subnet)s'),
                              {'name': t1_n[i]['name'],
                               'id': t1_n[i]['id'],
                               'subnet': t1_sub[i]['id']})
                    # Create T2 trunk network for this router
                    n_spec['network'].update(
                        {'name': constants.T2_NETWORK_NAME + indx})
                    t2_n.append(self._core_plugin.create_network(self._context,
                                                                 n_spec))
                    LOG.debug(_('Created T2 network with name %(name)s and '
                                'id %(id)s'),
                              {'name': constants.T2_NETWORK_NAME + indx,
                               'id': t2_n[i]['id']})
                    # Create dummy subnet for this trunk network
                    sub_spec['subnet'].update(
                        {'name': constants.T2_SUBNET_NAME + indx,
                         'network_id': t2_n[i]['id']})
                    t2_sub.append(self._core_plugin.create_subnet(self._context,
                                                                  sub_spec))

                    # Create T2 port for this router
                    p_spec['port'].update(
                        {'name': constants.T2_PORT_NAME + indx,
                         'network_id': t2_n[i]['id'],
                         'fixed_ips': [{'subnet_id': t2_sub[i]['id']}]})
                    t2_p.append(self._core_plugin.create_port(self._context,
                                                              p_spec))
                    LOG.debug(_('Created T2 port with name %(name)s,  '
                                'id %(id)s and subnet %(subnet)s'),
                              {'name': t2_n[i]['name'],
                               'id': t2_n[i]['id'],
                               'subnet': t2_sub[i]['id']})
            except q_exc.QuantumException:
                self.cleanup_for_service_vm_ovs(mgmt_port, t1_n, t1_sub, t1_p,
                                                t2_n, t2_sub, t2_p)
                mgmt_port = None
                t1_n, t1_p, t2_n, t2_p = [], [], [], []
        return mgmt_port, t1_n, t1_sub, t1_p, t2_n, t2_sub, t2_p

    def _unique_cfgdrive_filename(self, pre, uuid, suff=''):
        end = constants.CFG_DRIVE_UUID_START + constants.CFG_DRIVE_UUID_LEN
        return pre + uuid[constants.CFG_DRIVE_UUID_START:end] + suff

    def generate_config_for_csr(self, mgmtport):
        mgmt_ip = mgmtport['fixed_ips'][0]['ip_address']
        subnet_data = self._core_plugin.get_subnet(
            self._context, mgmtport['fixed_ips'][0]['subnet_id'],
            ['cidr', 'gateway_ip', 'dns_nameservers'])
        netmask = str(netaddr.IPNetwork(subnet_data['cidr']).netmask)
        params = {'<ip>': mgmt_ip, '<mask>': netmask,
                  '<gw>': subnet_data['gateway_ip'],
                  '<name_server>': '8.8.8.8'}
        try:
            config_template_file = (q_conf.CONF.templates_path + "/" +
                                    q_conf.CONF.csr_config_template)
            vm_instance_cfg_file = self._unique_cfgdrive_filename(
                q_conf.CONF.service_vm_config_path + "/csr_", mgmtport['id'],
                ".cfg")
            cfg_template = open(config_template_file, 'r')
            vm_instance_cfg = open(vm_instance_cfg_file, "w")
            # insert proper instance values in the template
            for line in cfg_template:
                tokens = line.strip('\n').split(' ')
                result = [params[token] if token in params.keys()
                          else token for token in tokens]
                line = ' '.join(map(str, result)) + '\n'
                vm_instance_cfg.write(line)
            vm_instance_cfg.close()
            cfg_template.close()
            return vm_instance_cfg_file
        except IOError as e:
            LOG.error(_('Failed to create config file: %s'), str(e))
            raise

    def delete_config_file_for_csr(self, uuid):
        vm_instance_cfg_file = self._unique_cfgdrive_filename(
            q_conf.CONF.service_vm_config_path + "/csr_", uuid,
            ".cfg")
        try:
            os.remove(vm_instance_cfg_file)
        except OSError as e:
            LOG.error(_('Failed to delete config file: %s'), str(e))

    # TODO(bob-melander): Move this to fake_service_vm_lib.py file
    # with FakeServiceVMManager
    def dispatch_service_vm_fake(self, vm_image, vm_flavor, mgmt_port, ports):
        vm_id = uuidutils.generate_uuid()

        if mgmt_port is not None:
            p_dict = {'port': {'device_id': vm_id,
                               'device_owner': 'nova'}}
            self._core_plugin.update_port(self._context, mgmt_port['id'],
                                          p_dict)

        for port in ports:
            p_dict = {'port': {'device_id': vm_id,
                               'device_owner': 'nova'}}
            self._core_plugin.update_port(self._context, port['id'], p_dict)

        myserver = {'server': {'adminPass': "MVk5HPrazHcG",
                    'id': vm_id,
                    'links': [{'href': "http://openstack.example.com/v2/"
                                        "openstack/servers/" + vm_id,
                               'rel': "self"},
                                {'href': "http://openstack.example.com/"
                                          "openstack/servers/" + vm_id,
                                 'rel': "bookmark"}]}}

        return myserver['server']

    def delete_service_vm_fake(self, id, mgmt_nw_id, delete_networks=False):
        ports = self._core_plugin.get_ports(self._context,
                                            filters={'device_id': [id]})

        nets_to_delete = []
        for port in ports:
            if delete_networks and port['network_id'] != mgmt_nw_id:
                nets_to_delete.append(port['network_id'])
            self._core_plugin.delete_port(self._context, port['id'])
        for net_id in nets_to_delete:
            self._core_plugin.delete_network(self._context, net_id)
        return True





