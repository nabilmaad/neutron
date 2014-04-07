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

from sqlalchemy.orm import exc

from neutron.db import db_base_plugin_v2 as base_db
from neutron import manager
from neutron.openstack.common import log as logging
from neutron.openstack.common import uuidutils
from neutron.plugins.cisco.l3.db.l3_models import RouterType
import neutron.plugins.cisco.l3.extensions.router_type as router_type

LOG = logging.getLogger(__name__)


class RouterTypesDbMixin(router_type.RouterTypePluginBase,
                         base_db.CommonDbMixin):
    """Mixin class for Router types."""

    @property
    def _core_plugin(self):
        return manager.NeutronManager.get_plugin()

    def _make_router_type_dict(self, router_type, fields=None):
        res = {'id': router_type['id'],
               'name': router_type['name'],
               'description': router_type['description'],
               'template_id': router_type['template_id'],
               'slot_need': router_type['slot_need'],
               'scheduler': router_type['scheduler'],
               'cfg_agent_driver': router_type['cfg_agent_driver']}
        return self._fields(res, fields)

    def create_router_type(self, context, router_type):
        """Creates a router type.

         Also binds it to the specified hosting device template.
         """
        LOG.debug("create_router_type() called. Contents %s", router_type)
        r = router_type['router_type']
        with context.session.begin(subtransactions=True):
            router_type_db = RouterType(id=uuidutils.generate_uuid(),
                                        name=r['name'],
                                        description=r['description'],
                                        template_id=r['template_id'],
                                        slot_need=r['slot_need'],
                                        scheduler=r['scheduler'],
                                        cfg_agent_driver=r['cfg_agent_driver'])
            context.session.add(router_type_db)
        return self._make_router_type_dict(router_type_db)

    def update_router_type(self, context, id, router_type):
        LOG.debug(_("update_router_type() called"))
        rt = router_type['router_type']
        with context.session.begin(subtransactions=True):
            rt_query = context.session.query(
                RouterType).with_lockmode('update')
            rt_db = rt_query.filter_by(id=id).one()
            rt_db.update(rt)
        return self._make_router_type_dict(rt_db)

    def delete_router_type(self, context, id):
        LOG.debug(_("delete_router_type() called"))
        with context.session.begin(subtransactions=True):
            router_type_query = context.session.query(
                RouterType).with_lockmode('update')
            router_type_db = router_type_query.filter_by(id=id).one()
            context.session.delete(router_type_db)

    def get_router_type(self, context, id, fields=None):
        LOG.debug(_("get_router_type() called"))
        try:
            query = self._model_query(context, RouterType)
            rt = query.filter(RouterType.id == id).one()
            return self._make_router_type_dict(rt, fields)
        except exc.NoResultFound:
            raise router_type.RouterTypeNotFound(router_type_id=id)

    def get_router_types(self, context, filters=None, fields=None,
                         sorts=None, limit=None, marker=None,
                         page_reverse=False):
        LOG.debug(_("get_router_types() called"))
        return self._get_collection(context, RouterType,
                                    self._make_router_type_dict,
                                    filters=filters, fields=fields,
                                    sorts=sorts, limit=limit,
                                    marker_obj=marker,
                                    page_reverse=page_reverse)

