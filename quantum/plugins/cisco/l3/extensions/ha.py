# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2013 OpenStack Foundation.
# All rights reserved.
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

from quantum.api import extensions
from quantum.api.v2 import attributes
from quantum.common import exceptions as qexception


# HA exceptions
class HADisabled(qexception.QuantumException):
    message = _("HA support is disabled")


class HAOnlyForGatewayRouters(qexception.QuantumException):
    message = _("%(msg)s")


class HADisabledHAType(qexception.QuantumException):
    message = _("HA type %(type)s is administratively disabled")


class HARedundancyLevel(qexception.QuantumException):
    message = _("Redundancy level for HA must be 1, 2, or 3")


class HATypeCannotBeChanged(qexception.QuantumException):
    message = _("HA type cannot be changed for a router with HA enabled")


class HATypeNotCompatibleWithFloatingIP(qexception.QuantumException):
    message = _("HA type %(type) cannot be used with FloatingIP")


HA_ENABLED = 'ha:enabled'
TYPE = 'ha:type'
REDUNDANCY_LEVEL = 'ha:redundancy_level'
PROBE_CONNECTIVITY = 'ha:probe_connectivity'
PING_TARGET = 'ha:ping_target'
PING_INTERVAL = 'ha:ping_interval'
HA_VRRP = 'VRRP'
HA_HSRP = 'HSRP'
HA_GLBP = 'GLBP'
HA_TYPES = [HA_VRRP, HA_HSRP, HA_GLBP]
MIN_REDUNDANCY_LEVEL = 1
MAX_REDUNDANCY_LEVEL = 3

EXTENDED_ATTRIBUTES_2_0 = {
    'routers': {
        HA_ENABLED: {'allow_post': True, 'allow_put': True,
                     'convert_to': attributes.convert_to_boolean,
                     'default': attributes.ATTR_NOT_SPECIFIED,
                     'is_visible': True},
        TYPE: {'allow_post': True, 'allow_put': True,
               'validate': {'type:values': HA_TYPES},
               'default': attributes.ATTR_NOT_SPECIFIED,
               'is_visible': True},
        REDUNDANCY_LEVEL: {'allow_post': True, 'allow_put': True,
                           'convert_to': attributes.convert_to_int,
                           'validate': {'type:range': [MIN_REDUNDANCY_LEVEL,
                                                       MAX_REDUNDANCY_LEVEL]},
                           'default': attributes.ATTR_NOT_SPECIFIED,
                           'is_visible': True},
        PROBE_CONNECTIVITY: {'allow_post': True, 'allow_put': True,
                             'convert_to': attributes.convert_to_boolean,
                             'default': attributes.ATTR_NOT_SPECIFIED,
                             'is_visible': True},
        PING_TARGET: {'allow_post': True, 'allow_put': True,
                      'validate': {'type:ip_address': None},
                      'default': attributes.ATTR_NOT_SPECIFIED,
                      'is_visible': True},
        PING_INTERVAL: {'allow_post': True, 'allow_put': True,
                        'convert_to': attributes.convert_to_int,
                        'validate': attributes._validate_non_negative,
                        'default': attributes.ATTR_NOT_SPECIFIED,
                        'is_visible': True}
    }
}


class Ha(extensions.ExtensionDescriptor):
    """Extension class to support HA by VRRP, HSRP and GLBP.

    This class is used by Neutron's extension framework to support
    HA redundancy by VRRP, HSRP and GLBP for Neutron Routers.

    Attribute 'ha_type' can be one of 'vrrp', 'hsrp' and 'glbp'
    Attribute 'redundancy_level' specifies the number of routers
              added for redundancy and can be 1, 2, or 3.

    To create a router with HSRP-based HA with 2 extra routers
    for redundancy using the CLI with admin rights:

       (shell) router-create <router_name> --ha:ha_type hsrp \
       --ha:redundancy_level 2
    """

    @classmethod
    def get_name(cls):
        return "high-availability for routing service"

    @classmethod
    def get_alias(cls):
        return "ha"

    @classmethod
    def get_description(cls):
        return "High availability by VRRP, HSRP, and GLBP"

    @classmethod
    def get_namespace(cls):
        return "http://docs.openstack.org/ext/ha/api/v1.0"

    @classmethod
    def get_updated(cls):
        return "2013-12-07T10:00:00-00:00"

    def get_extended_resources(self, version):
        if version == "2.0":
            return EXTENDED_ATTRIBUTES_2_0
        else:
            return {}
