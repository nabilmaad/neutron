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
# @author: Hareesh Puthalath, Cisco Systems, Inc.

from abc import abstractmethod

from neutron.api import extensions
from neutron.api.v2 import attributes
from neutron.api.v2 import base
from neutron.api.v2 import resource_helper
from neutron.common import exceptions
from neutron import manager
from neutron.openstack.common import importutils
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants

LOG = logging.getLogger(__name__)


class DriverNotFound(exceptions.NetworkNotFound):
    message = _("Driver %(driver)s does not exist")


class SchedulerNotFound(exceptions.NetworkNotFound):
    message = _("Scheduler %(scheduler)s does not exist")


def convert_validate_import(import_obj):
    if import_obj is None:
        raise DriverNotFound(driver=import_obj)
    try:
        kwargs = {}
        importutils.import_object(import_obj, **kwargs)
        return import_obj
    except ImportError:
        raise DriverNotFound(driver=import_obj)
    except Exception:
        return import_obj

NAME = 'router_type'
TYPE = NAME + ':id'
ROUTER_TYPES = NAME + 's'

RESOURCE_ATTRIBUTE_MAP = {
    ROUTER_TYPES: {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:uuid': None},
               'is_visible': True,
               'primary_key': True},
        'name': {'allow_post': True, 'allow_put': True,
                 'validate': {'type:string': None},
                 'is_visible': True, 'default': ''},
        'description': {'allow_post': True, 'allow_put': True,
                        'validate': {'type:string': None},
                        'is_visible': True, 'default': ''},
        'tenant_id': {'allow_post': True, 'allow_put': False,
                      'required_by_policy': True,
                      'is_visible': True},
        'template_id': {'allow_post': True, 'allow_put': False,
                        'validate': {'type:uuid': None},
                        'is_visible': True},
        'slot_need': {'allow_post': True, 'allow_put': True,
                      'required_by_policy': True,
                      'validate': {'type:non_negative': None},
                      'is_visible': True},
        'scheduler': {'allow_post': True, 'allow_put': False,
                      'required_by_policy': True,
                      'convert_to': convert_validate_import,
                      'is_visible': True},
        'cfg_agent_driver': {'allow_post': True, 'allow_put': False,
                             'required_by_policy': True,
                             'convert_to': convert_validate_import,
                             'is_visible': True},
    }
}

EXTENDED_ATTRIBUTES_2_0 = {
    'routers': {
        TYPE: {'allow_post': True, 'allow_put': True,
               'validate': {'type:string': None},
               'default': attributes.ATTR_NOT_SPECIFIED,
               'is_visible': True},
    }
}

class Router_type(extensions.ExtensionDescriptor):
    """Extension class to define different types of Neutron routers.

    This class is used by Neutron's extension framework to support
    definition of different types of Neutron Routers.

    Attribute 'router_type:id' is the uuid or name of a certain router type.
    It can be set during creation of Neutron router. If a Neutron router is
    moved (by admin user) to a hosting device of a different hosting device
    type, the router type of the Neutron router will also change. Non-admin
    users can request that a Neutron router's type is changed.

    To create a router of router type <name>:

       (shell) router-create <router_name> --router_type:id <uuid_or_name>
    """

    @classmethod
    def get_name(cls):
        return "Router types for routing service"

    @classmethod
    def get_alias(cls):
        return NAME

    @classmethod
    def get_description(cls):
        return "Introduces router_type attribute for Neutron Routers"

    @classmethod
    def get_namespace(cls):
        return "http://docs.openstack.org/ext/" + NAME + "/api/v1.0"

    @classmethod
    def get_updated(cls):
        return "2014-02-07T10:00:00-00:00"

    @classmethod
    def get_resources(cls):
        """Returns Ext Resources."""
        exts = []
        my_plurals = [(key, key[:-1]) for key in RESOURCE_ATTRIBUTE_MAP.keys()]
        attributes.PLURALS.update(dict(my_plurals))
        plugin = manager.NeutronManager.get_plugin()
        collection_name = ROUTER_TYPES
        params = RESOURCE_ATTRIBUTE_MAP.get(ROUTER_TYPES, dict())
        # quota.QUOTAS.register_resource_by_name(resource_name)
        controller = base.create_resource(collection_name,
                                          NAME,
                                          plugin, params, allow_bulk=True,
                                          allow_pagination=True,
                                          allow_sorting=True)
        ex = extensions.ResourceExtension(collection_name,
                                          controller,
                                          attr_map=params)
        exts.append(ex)
        return exts

        # plurals = [(key, key[:-1]) for key in RESOURCE_ATTRIBUTE_MAP.keys()]
        # attributes.PLURALS.update(plurals)
        # return resource_helper.build_resource_info(plurals,
        #                                            RESOURCE_ATTRIBUTE_MAP,
        #                                            constants.L3_ROUTER_NAT)

    def get_extended_resources(self, version):
        if version == "2.0":
            return EXTENDED_ATTRIBUTES_2_0
        else:
            return {}


# router_type exceptions
class UndefinedRouterType(exceptions.NeutronException):
    message = _("Router type %(type) does not exist")


class RouterTypeAlreadyDefined(exceptions.NeutronException):
    message = _("Router type %(type) already exists")


class NoSuchHostingDeviceTemplateForRouterType(exceptions.NeutronException):
    message = _("No hosting device template with id %(type) exists")


class HostingDeviceTemplateUsedByRouterType(exceptions.NeutronException):
    message = _("Router type %(type) already defined for Hosting device "
                "template with id %(type)")


class RouterTypeHasRouters(exceptions.NeutronException):
    message = _("Router type %(type) cannot be deleted since routers "
                "of that type exists")


class RouterTypeNotFound(exceptions.NotFound):
    message = _("RouterType %(router_type_id)s could not be found.")


class RouterTypePluginBase(object):
    """REST API to manage router types.

    All methods except listing require admin context.
    """

    @abstractmethod
    def create_router_type(self, context, router_type):
        """Creates a router type.
         Also binds it to the specified hosting device template.
         """
        pass

    @abstractmethod
    def update_router_type(self, context, id, router_type):
        """Updates a router type."""
        pass

    @abstractmethod
    def delete_router_type(self, context, id):
        """Deletes a router type."""
        pass

    @abstractmethod
    def get_router_type(self, context, id, fields=None):
        """Lists defined router type."""
        pass

    @abstractmethod
    def get_router_types(self, context, filters=None, fields=None,
                         sorts=None, limit=None, marker=None,
                         page_reverse=False):
        """Lists defined router types."""
        pass
