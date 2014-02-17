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

"""Exceptions by Cisco Configuration Agent."""

from neutron.common import exceptions


class DriverException(exceptions.NeutronException):
    """Exception created by the Driver class."""


class CSR1000vInitializationException(DriverException):
    """Exception when initialization of CSR1000v Routing Driver object."""
    message = (_("Critical device parameter missing. Failed initializing "
                 "CSR1000vRoutingDriver"))


class CSR1000vConnectionException(DriverException):
    """Connection exception when connecting to CSR1000v hosting device."""
    message = (_("Failed connecting to CSR1000v. Reason: %(reason)s. "
               "Connection Params are Host:%(host)s, "
               "Port:%(port)s, Device timeout:%(timeout)s"))


class CSR1000vConfigException(DriverException):
    """Configuration exception thrown when modifying the running config."""
    message = (_("Error executing snippet:%(snippet)s. "
                 "ErrorType:%(type)s ErrorTag:%(tag)s"))
