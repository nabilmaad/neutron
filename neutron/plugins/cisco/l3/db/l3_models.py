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

import sqlalchemy as sa
from sqlalchemy import orm

from neutron.db import agents_db
from neutron.db import l3_db
from neutron.db import model_base
from neutron.db import models_v2


class HostingDeviceTemplate(model_base.BASEV2, models_v2.HasId,
                            models_v2.HasTenant):
    """Represents a template for devices used to host service.

       Such devices may be physical or virtual.
    """
    # name given to hosting devices created using this template
    name = sa.Column(sa.String(255))
    # template enabled if True
    enabled = sa.Column(sa.Boolean, nullable=False, default=True)
    # 'host_category' can be 'VM', 'Hardware'
    host_category = sa.Column(sa.String(255), nullable=False)
    # 'host_type' can be 'NetworkNamespaceNode', 'CSR1kv', ...
    host_type = sa.Column(sa.String(255), nullable=False)
    # list of service types hosting devices based on this template support
    service_types = sa.Column(sa.String(255))
    # the image name or uuid in Glance
    image = sa.Column(sa.String(255))
    # the VM flavor or uuid in Nova
    flavor = sa.Column(sa.String(255))
    # 'configuration_mechanism' indicates how configurations are made
    configuration_mechanism = sa.Column(sa.String(255))
    # 'transport_port' is udp/tcp port of hosting device. May be empty.
    transport_port = sa.Column(sa.Integer)
    # Typical time (in seconds) needed for hosting device (created
    # from this template) to boot into operational state.
    booting_time = sa.Column(sa.Integer, default=0)
    # abstract metric specifying capacity to host logical resources
    slot_capacity = sa.Column(sa.Integer, nullable=False, autoincrement=False)
    # desired number of slots to keep available at all times
    desired_slots_free = sa.Column(sa.Integer, nullable=False, default=0,
                                   autoincrement=False)
    # 'tenant_bound' is empty or is a list ids of the only tenants allowed
    #  to own/place resources on hosting devices created using this template
    tenant_bound = sa.Column(sa.String(255))
    # module to be used as plugging driver for logical resources
    # hosted inside hosting devices created using this template
    device_driver = sa.Column(sa.String(255), nullable=False)
    # module to be used as hosting device driver when creating
    # hosting devices using his template
    plugging_driver = sa.Column(sa.String(255), nullable=False)


class DeviceCredential(model_base.BASEV2, models_v2.HasId):
    """Represents credentials to control Cisco devices."""

    name = sa.Column(sa.String(255))
    description = sa.Column(sa.String(255))
    user_name = sa.Column(sa.String(255))
    password = sa.Column(sa.String(255))
    type = sa.Column(sa.String(255))


class HostingDevice(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """Represents an appliance hosting Neutron router(s).

       When the hosting device is a Nova VM 'id' is uuid of that VM.
    """
    # id of hosting device template used to create the hosting device
    template_id = sa.Column(sa.String(36),
                            sa.ForeignKey('hostingdevicetemplates.id'))
    template = orm.relationship(HostingDeviceTemplate)
    # id of credentials for this hosting device
    credential_id = sa.Column(sa.String(36),
                              sa.ForeignKey('devicecredentials.id'))
    credentials = orm.relationship(DeviceCredential)
    # manufacturer id of the device, e.g., its serial number
    device_id = sa.Column(sa.String(255))
    # number of allocated slots in this hosting device
    allocated_slots = sa.Column(sa.Integer, autoincrement=False)
    admin_state_up = sa.Column(sa.Boolean, nullable=False, default=True)
    # 'ip_address' is address of hosting device's management interface
    ip_address = sa.Column(sa.String(64), nullable=False)
    # 'transport_port' is udp/tcp port of hosting device. May be empty.
    transport_port = sa.Column(sa.Integer)
    cfg_agent_id = sa.Column(sa.String(36),
                             sa.ForeignKey('agents.id'),
                             nullable=True)
    cfg_agent = orm.relationship(agents_db.Agent)
    # Service VMs take time to boot so we store creation time
    # so we can give preference to older ones when scheduling
    created_at = sa.Column(sa.DateTime, nullable=False)
    # Typical time (in seconds) needed for hosting device to boot
    # into operational state.
    booting_time = sa.Column(sa.Integer, default=0)
    status = sa.Column(sa.String(16))
    # 'tenant_bound' is empty or is id of the only tenant allowed to
    # own/place resources on this hosting device
    tenant_bound = sa.Column(sa.String(36))
    auto_delete_on_fail = sa.Column(sa.Boolean, default=True, nullable=False)


class RouterType(model_base.BASEV2, models_v2.HasId):
    """Represents Neutron router types.

    A router type is associated with a with hosting device template.
    The template is used when hosting device for the router type is created.

    Only 'id', 'name', 'description' are visible in non-admin context.
    """
    # name of router type, should preferably be unique
    name = sa.Column(sa.String(255), nullable=False, primary_key=True)
    # description of this router type
    description = sa.Column(sa.String(255))
    # template to use to create hosting devices for this router type
    template_id = sa.Column(sa.String(36),
                            sa.ForeignKey('hostingdevicetemplates.id',
                                          ondelete='CASCADE'),
                            primary_key=True)
    template = orm.relationship(HostingDeviceTemplate)
    # The number of slots this router type consume in hosting device
    slot_need = sa.Column(sa.Integer, autoincrement=False)
    # module to be used as scheduler for router of this type
    scheduler = sa.Column(sa.String(255), nullable=False)
    # module to be used by configuration agent for router of this type
    cfg_agent_driver = sa.Column(sa.String(255), nullable=False)


class RouterHostingDeviceBinding(model_base.BASEV2):
    """Represents binding between Neutron routers and their hosting devices."""
    router_id = sa.Column(sa.String(36),
                          sa.ForeignKey('routers.id', ondelete='CASCADE'),
                          primary_key=True)
    router = orm.relationship(l3_db.Router)
    # 'router_type_id' is id of router type for this router
    router_type_id = sa.Column(
        sa.String(36),
        sa.ForeignKey('routertypes.id'),
        primary_key=True,
        nullable=False)
    router_type = orm.relationship(RouterType)
    # 'inflated_slot_need' is the slot need of the router plus the
    # number slots needed by other resources to be associated with the
    # router. It's only considered if > 0.
    inflated_slot_need = sa.Column(sa.Integer, default=0, autoincrement=False)
    # If 'auto_schedule' is True then router is automatically scheduled
    # if it lacks a hosting device or its hosting device fails.
    auto_schedule = sa.Column(sa.Boolean, default=True, nullable=False)
    share_hosting_device = sa.Column(sa.Boolean, default=True, nullable=False)
    # id of hosting device hosting this router, None/NULL if unscheduled.
    hosting_device_id = sa.Column(sa.String(36),
                                  sa.ForeignKey('hostingdevices.id',
                                                ondelete='SET NULL'))
    hosting_device = orm.relationship(HostingDevice)


class HostedHostingPortBinding(model_base.BASEV2):
    """Represents binding of logical resource's port to its hosting port."""
    logical_resource_id = sa.Column(sa.String(36), primary_key=True)
    logical_port_id = sa.Column(sa.String(36),
                                sa.ForeignKey('ports.id',
                                              ondelete="CASCADE"),
                                primary_key=True)
    logical_port = orm.relationship(
        models_v2.Port,
        primaryjoin='Port.id==HostedHostingPortBinding.logical_port_id',
        backref=orm.backref('hosting_info', cascade='all', uselist=False))
    # type of router port: router_interface, ..._gateway, ..._floatingip
    port_type = sa.Column(sa.String(32))
    # type of network the router port belongs to
    network_type = sa.Column(sa.String(32))
    hosting_port_id = sa.Column(sa.String(36),
                                sa.ForeignKey('ports.id',
                                              ondelete='SET NULL'))
    hosting_port = orm.relationship(
        models_v2.Port,
        primaryjoin='Port.id==HostedHostingPortBinding.hosting_port_id')
    # VLAN tag for trunk ports
    segmentation_tag = sa.Column(sa.Integer, autoincrement=False)
