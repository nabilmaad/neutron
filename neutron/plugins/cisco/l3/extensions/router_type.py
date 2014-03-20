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

from abc import abstractmethod

import webob.exc

from neutron.api import extensions
from neutron.api.v2 import attributes
from neutron.api.v2 import base
from neutron.api.v2 import resource
from neutron.common import exceptions
from neutron import manager
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants as service_constants
from neutron import wsgi

LOG = logging.getLogger(__name__)


NAME = 'router_type'
TYPE = NAME + ':id'
ROUTER_TYPES = TYPE + 's'

EXTENDED_ATTRIBUTES_2_0 = {
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
        'template_id': {'allow_post': True, 'allow_put': False,
                        'validate': {'type:uuid': None},
                        'is_visible': True},
        'slot_need': {'allow_post': True, 'allow_put': True,
                      'required_by_policy': True,
                      'validate': {'type:non_negative': None},
                      'is_visible': True},
        'scheduler': {'allow_post': True, 'allow_put': False,
                      'required_by_policy': True,
                      'validate': {'type:string': None},
                      'is_visible': True},
        'cfg_agent_driver': {'allow_post': True, 'allow_put': False,
                             'required_by_policy': True,
                             'validate': {'type:string': None},
                             'is_visible': True},
    },
    'routers': {
        TYPE: {'allow_post': True, 'allow_put': True,
               'validate': {'type:string': None},
               'default': attributes.ATTR_NOT_SPECIFIED,
               'is_visible': True},
    }
}


class RouterTypeController(wsgi.Controller):
    def get_plugin(self):
        plugin = manager.NeutronManager.get_service_plugins().get(
            service_constants.L3_ROUTER_NAT)
        if not plugin or not utils.is_extension_supported(plugin, NAME):
            LOG.error(_('No plugin for L3 routing registered to handle '
                        'router type resources'))
            msg = _('The resource could not be found.')
            raise webob.exc.HTTPNotFound(msg)
        return plugin

    def index(self, request, **kwargs):
        plugin = self.get_plugin()
        return plugin.get_router_types(request.context, **kwargs)


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
        parent = dict(member_name="router",
                      collection_name="routers")
        controller = resource.Resource(RouterTypeController(),
                                       base.FAULT_MAP)
        exts.append(extensions.ResourceExtension(
            ROUTER_TYPES, controller, parent))
        return exts

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
    def update_router_type(self, context, router_type):
        """Updates a router type."""
        pass

    @abstractmethod
    def delete_router_type(self, context, id):
        """Deletes a router type."""
        pass

    @abstractmethod
    def get_router_type(self, context, id, fields=None):
        """Lists defined router types."""
        pass

    @abstractmethod
    def get_router_types(self, context, filters=None, fields=None,
                         sorts=None, limit=None, marker=None,
                         page_reverse=False):
        """Lists defined router types."""
        pass
