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

N1KV_PLUGIN = 1
OVS_PLUGIN = 2
ML2_PLUGIN = 3

# Property for devices listed in plugin configuration file
# that is set to True if the device supports routing.
SUPPORTS_ROUTING = 'supports_routing'
DEVICE_TYPE = 'device_type'

# T1 port/network is for VXLAN
T1_PORT_NAME = 't1_p:'
# T2 port/network is for VLAN
T2_PORT_NAME = 't2_p:'
T1_NETWORK_NAME = 't1_n:'
T2_NETWORK_NAME = 't2_n:'
T1_SUBNET_NAME = 't1_sn:'
T2_SUBNET_NAME = 't2_sn:'

# These prefix defines will go away when Nova allows spinning up
# VMs with vifs on networks without subnet(s).
SUB_PREFX = '172.16.1.0/24'

T1_SUBNET_START_PREFX = '172.16.'
T2_SUBNET_START_PREFX = '172.32.'

# Hosting entities are of one of the following types.
NETWORK_NODE_HOST = 'NetworkNamespaceNode'
CSR1KV_HOST = 'CSR1kv'
# For Nexus series (NX-OS) top of rack switches
NEXUS_TOR_HOST = 'NexusToR'
# For Nexus series (NX-OS) aggregation switches
NEXUS_AGGR_HOST = 'NexusAggregationSwitch'

# Router type is a new attribute for OsN Router
# It can be set in Create operation then just Read.
# Router type is instead changed by moving the
# router to a hosting entity of another type.
CSR_ROUTER_TYPE = 'CSR1kv'
NAMESPACE_ROUTER_TYPE = 'NetworkNamespace'
HARDWARE_ROUTER_TYPE = 'Hardware'

CSR1kv_SSH_NETCONF_PORT = 22
NEXUS_SSH_NETCONF_PORT = 22

# Time needed (in seconds) to boot a device
CSR_BOOTING_TIME = 300
NEXUS_BOOTING_TIME = 300

AGENT_TYPE_L3_CFG = 'L3 cfg agent'

# Topic for L3 configuration agent
L3_CFG_AGENT = 'l3_cfg_agent'

# Length mgmt port UUID to be part of VM's config drive filename
CFG_DRIVE_UUID_START = 24
CFG_DRIVE_UUID_LEN = 12

# Service VM status
SVM_OK = 'OK'
SVM_ERROR = 'ERROR'
SVM_NON_RESPONSIVE = 'NON_RESPONSIVE'