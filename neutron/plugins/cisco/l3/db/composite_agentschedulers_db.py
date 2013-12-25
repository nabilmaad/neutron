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

from oslo.config import cfg
from sqlalchemy.orm import joinedload

from neutron.common import constants
from neutron.db import agents_db
from neutron.db import agentschedulers_db as agentsched_db
from neutron.openstack.common import log as logging
from neutron.openstack.common import timeutils
from neutron.plugins.cisco.l3.common import constants as cl3_constants
from neutron.plugins.cisco.l3.db import l3_router_appliance_db as l3_ra_db


LOG = logging.getLogger(__name__)


class CompositeAgentSchedulerDbMixin(agentsched_db.AgentSchedulerDbMixin):
    """Mixin class to add agent scheduler extension to db_plugin_base_v2.
    This class also supports l3 configuration agents."""

    @classmethod
    def is_agent_down(cls, heart_beat_time,
                      timeout=cfg.CONF.cfg_agent_down_time):
        return timeutils.is_older_than(heart_beat_time, timeout)

    def auto_schedule_hosting_entities_on_cfg_agent(self, context, host,
                                                    router_id):
        # There may be routers that have not been scheduled
        # on a hosting entity so we try to do that now
        self.host_router(context, router_id)
        if self.router_scheduler:
            return (self.router_scheduler.
                    auto_schedule_hosting_entities_on_cfg_agent(context, host,
                                                                router_id))

    def list_active_sync_routers_on_active_l3_cfg_agent(self, context, host,
                                                        router_id,
                                                        hosting_entity_ids=[]):
        agent = self._get_agent_by_type_and_host(
            context, cl3_constants.AGENT_TYPE_L3_CFG, host)

        if not agent.admin_state_up:
            return []
        query = context.session.query(
            l3_ra_db.RouterHostingEntityBinding.router_id)
        query = query.join(l3_ra_db.HostingEntity)
        query = query.filter(
            l3_ra_db.HostingEntity.l3_cfg_agent_id == agent.id)
        if router_id:
            query = query.filter(
                l3_ra_db.RouterHostingEntityBinding.router_id == router_id)
        if len(hosting_entity_ids) == 1:
            query = query.filter(
                l3_ra_db.RouterHostingEntityBinding.hosting_entity_id ==
                hosting_entity_ids[0])
        elif len(hosting_entity_ids) > 1:
            query = query.filter(
                l3_ra_db.RouterHostingEntityBinding.hosting_entity_id.in_(
                    hosting_entity_ids))
        router_ids = [item[0] for item in query]
        if router_ids:
            return self.get_sync_data_ext(context, router_ids=router_ids,
                                          active=True)
        else:
            return []

    def add_hosting_entity_to_l3_cfg_agent(self, context, agent_id,
                                           hosting_entity_id):
        #TODO(bobmel): Implement the adding to agent
        pass

    def remove_hosting_entity_from_l3_cfg_agent(self, context, agent_id,
                                                hosting_entity_id):
        #TODO(bobmel): Implement the removal from agent
        pass

    def list_hosting_entities_on_l3_cfg_agent(self, context, agent_id):
        #TODO(bobmel): Change so it returns correct hosting entities
        return {'hosting_entities': []}

    def list_l3_cfg_agents_for_hosting_entity(self, context,
                                              hosting_entity_id):
        #TODO(bobmel): Change so it returns correct agent
        return {'l3_cfg_agents': []}

    def get_l3_cfg_agents(self, context, active=None, filters=None):
        query = context.session.query(agents_db.Agent)
        query = query.filter(
            agents_db.Agent.agent_type == cl3_constants.AGENT_TYPE_L3_CFG)
        if active is not None:
            query = (query.filter(agents_db.Agent.admin_state_up == active))
        if filters:
            for key, value in filters.iteritems():
                column = getattr(agents_db.Agent, key, None)
                if column:
                    query = query.filter(column.in_(value))
        l3_cfg_agents = query.all()
        if active is not None:
            l3_cfg_agents = [l3_cfg_agent for l3_cfg_agent in
                             l3_cfg_agents if not
                             self.is_agent_down(
                                 l3_cfg_agent['heartbeat_timestamp'])]
        return l3_cfg_agents

    def get_l3_cfg_agents_for_hosting_entities(self, context,
                                               hosting_entity_ids,
                                               admin_state_up=None,
                                               active=None):
        if not hosting_entity_ids:
            return []
        query = context.session.query(l3_ra_db.HostingEntity)
        if len(hosting_entity_ids) > 1:
            query = query.options(joinedload('l3_cfg_agent')).filter(
                l3_ra_db.HostingEntity.id.in_(hosting_entity_ids))
        else:
            query = query.options(joinedload('l3_cfg_agent')).filter(
                l3_ra_db.HostingEntity.id == hosting_entity_ids[0])
        if admin_state_up is not None:
            query = (query.filter(agents_db.Agent.admin_state_up ==
                                  admin_state_up))
        agents = [hosting_entity.l3_cfg_agent for hosting_entity in query
                  if hosting_entity.l3_cfg_agent is not None]
        if active is not None:
            agents = [agent for agent in agents if not
                      self.is_agent_down(agent['heartbeat_timestamp'])]
        return agents

    def update_agent(self, context, id, agent):
        original_agent = self.get_agent(context, id)
        # Call parent of our parent to avoid
        result = super(agentsched_db.AgentSchedulerDbMixin, self).update_agent(
            context, id, agent)
        agent_data = agent['agent']
        if ('admin_state_up' in agent_data and
                original_agent['admin_state_up'] !=
                agent_data['admin_state_up']):
            if (original_agent['agent_type'] == constants.AGENT_TYPE_DHCP and
                self.dhcp_agent_notifier):
                self.dhcp_agent_notifier.agent_updated(
                    context, agent_data['admin_state_up'],
                    original_agent['host'])
            elif (original_agent['agent_type'] == constants.AGENT_TYPE_L3 and
                  self.l3_agent_notifier):
                self.l3_agent_notifier.agent_updated(
                    context, agent_data['admin_state_up'],
                    original_agent['host'])
            elif (original_agent['agent_type'] ==
                  cl3_constants.AGENT_TYPE_L3_CFG and self.l3_agent_notifier):
                self.l3_agent_notifier.agent_updated(
                    context, agent_data['admin_state_up'],
                    original_agent['host'])
        return result