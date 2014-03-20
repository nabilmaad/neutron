#!/bin/bash

# Default values
# --------------
# adminUser is same as name of Openstack network service,
# i.e., it should be either 'neutron' or 'quantum', for
# release >=Havana and release <=Grizzly, respectively.
adminUser=${1:-neutron}
osn=$adminUser
plugin=${2:-n1kv}
localrc=$3


if [[ ! -z $localrc && -f $localrc ]]; then
    eval $(grep ^Q_CISCO_CSR1KV_QCOW2_IMAGE= $localrc)
fi

l3AdminTenant="L3AdminTenant"
csr1kvFlavorName="csr1kv_router"
csr1kvFlavorId=621
networkHostsAggregateName="compute_network_hosts"
aggregateMetadataKey="aggregate_instance_extra_specs:network_host"
aggregateMetadataValue="True"
aggregateMetadata="$aggregateMetadataKey=$aggregateMetadataValue"
computeNetworkNodes=($(hostname) ComputeNode)
csr1kvImageSrc=$Q_CISCO_CSR1KV_QCOW2_IMAGE
csr1kvImageName="csr1kv_openstack_img"
csr1kvDiskFormat="qcow2"
csr1kvContainerFormat="bare"
csr1kvGlanceExtraParams="--property hw_vif_model=e1000 --property hw_disk_bus=ide --property hw_cdrom_bus=ide"
csr1kvHostingDeviceTemplateName="CSR1kv_template"


tenantId=`keystone tenant-get $l3AdminTenant 2>&1 | awk '/No tenant|id/ { if ($1 == "No") print "No"; else print $4; }'`
if [ "$tenantId" == "No" ]; then
   echo "No $l3AdminTenant exists, please create one using the setup_keystone... script then re-run this script."
   echo "Aborting!"
   exit 1
fi


source ~/devstack/openrc $adminUser $L3AdminTenant


echo -n "Checking if flavor '$csr1kvFlavorName' exists ..."
flavorId=`nova flavor-show $csr1kvFlavorId 2>&1 | awk '
/No flavor|id|endpoint/ {
   if ($index ("endpoint", $0) > 0) {
      print "NO SERVER"; nextfile;
   }
   else if ($2 == "No")
      print "No";
   else
      print $4;
}'`

if [ "$flavorId" == "No" ]; then
   echo " No, it does not. Creating it."
   flavorId=`nova flavor-create $csr1kvFlavorName $csr1kvFlavorId 8192 0 4 --is-public False | awk -v r=$csr1kvFlavorName '$0 ~ r { print $2 }'`
elif [ "$flavorId" == "NO SERVER" ]; then
   echo " Nova does not seem to be running. Skipping!"
else
   echo " Yes, it does."
fi

if [ "$flavorId" != "NO SERVER" ]; then
    echo -n "Checking if flavor '$csr1kvFlavorName' has metadata '$aggregateMetadata' ..."
    hasMetadata=`nova flavor-show 621 2>&1 | awk -v key=$aggregateMetadataKey -v value=$aggregateMetadataValue '
    BEGIN { res = "No" }
    {
       if ($2 == "extra_specs" && index($4, key) > 0  && index($5, value) > 0)
         res = "Yes"
    }
    END { print res }'`

    if [ "$hasMetadata" == "No" ]; then
       echo " No, it does not. Adding it."
       nova flavor-key $csr1kvFlavorId set $aggregateMetadata > /dev/null 2>&1
    else
       echo " Yes, it does."
    fi


    echo -n "Checking if aggregate '$networkHostsAggregateName' exists ..."
    aggregateId=`nova aggregate-list 2>&1 | awk -v name=$networkHostsAggregateName -v r=$networkHostsAggregateName"|Id" '
    BEGIN { res = "No" }
    $0 ~ r {
      if ($2 != "Id" && $4 == name)
        res = $2;
    }
    END { print res; }'`

    if [ "$aggregateId" == "No" ]; then
       echo " No, it does not. Creating it."
       aggregateId=`nova aggregate-create $networkHostsAggregateName 2>&1 | awk -v name=$networkHostsAggregateName -v r=$networkHostsAggregateName"|Id" 'BEGIN { res = "No" } $0 ~ r { if ($2 != "Id" && $4 == name) res = $2; } END { print res; }'`
    else
       echo " Yes, it does."
    fi

    echo "Setting metadata for aggregate '$networkHostsAggregateName'"
    nova aggregate-set-metadata $aggregateId $aggregateMetadata > /dev/null 2>&1

    echo "Configuring compute nodes to act as network hosts ..."

    for host in ${computeNetworkNodes[*]}
    do
       host_exists=`nova host-describe $host 2>&1 | awk 'BEGIN { res = "Yes" } /ERROR/ { if ($1 == "ERROR:") res = "No"; } END { print res; } '`
       if [ "$host_exists" == "Yes" ]; then
           host_added=`nova aggregate-details $aggregateId 2>&1 | awk -v host=$host 'BEGIN { res = "No" } { if (index($8, host) > 0) res = "Yes"; } END { print res }'`
           if [ "$host_added" == "No" ]; then
               echo "    Adding host '$host' to '$networkHostsAggregateName' aggregate"
               nova aggregate-add-host $aggregateId $host > /dev/null 2>&1
           else
               echo "    Skipping host '$host' since it has already been added"
           fi
       else
           echo "    Skipping host '$host' which is not up"
       fi
    done

    echo "Removing relevant quota limits ..."
    nova quota-update --cores -1 --instances -1 --ram -1 $tenantId > /dev/null 2>&1
fi

echo -n "Checking if image '$csr1kvImageName' exists ..."
hasImage=`glance image-show $csr1kvImageName 2>&1 | awk '
/Property|No|endpoint/ {
   if ($index ("endpoint", $0) > 0) {
      print "NO SERVER"; nextfile;
   }
   else if ($1 == "No")
      print "No";
   else
      print "Yes";
}'`

if [ "$hasImage" == "No" ]; then
   echo " No, it does not. Creating it."
   glance image-create --name $csr1kvImageName --owner $tenantId --disk-format $csr1kvDiskFormat --container-format $csr1kvContainerFormat --file $csr1kvImageSrc $csr1kvGlanceExtraParams
elif [ "$hasImage" == "NO SERVER" ]; then
   echo " Glance does not seem to be running. Skipping!"
else
   echo " Yes, it does."
fi


echo -n "Checking if '$csr1kvHostingDeviceTemplateName' is registered as hosting device template in $osn ..."
if [ "$plugin" == "n1kv" ]; then
   db="cisco_$osn"
   hd_driver="neutron.plugins.cisco.l3.hosting_device_drivers.csr1kv_hd_driver.CSR1kvHostingDeviceDriver"
   plugging_driver="neutron.plugins.cisco.l3.plugging_drivers.n1kv_trunking_driver.N1kvTrunkingPlugDriver"
else
   db="csr1kv_ovs_$osn"
   hd_driver="neutron.plugins.cisco.l3.hosting_device_drivers.csr1kv_hd_driver.CSR1kvHostingDeviceDriver"
   plugging_driver="neutron.plugins.cisco.l3.plugging_drivers.ovs_trunking_driver.OvsTrunkingPlugDriver"
fi
sql_statement="SELECT id FROM hostingdevicetemplates where id='11111111-2222-3333-4444-555555555555'"
hasTemplate=`mysql -e "use $db; $sql_statement" | awk '/id/ { print "Yes" }'`

if [ "$hasTemplate" != "Yes" ]; then
   echo " No, it is not. Registering it."

   # Columns: tenant_id, id, name, enabled, host_category, host_type, service_types, image, flavor, configurations_mechanism, transport_port, booting_time, slot_capacity, desired_slots_free, tenant_bound, device_driver, plugging_driver
   sql_statement="INSERT INTO hostingdevicetemplates VALUES
   ('$tenantId', '11111111-2222-3333-4444-555555555555',
    '$csr1kvHostingDeviceTemplateName', TRUE, 'VM', 'CSR1kv', 'router',
    '$csr1kvImageName', '$csr1kvFlavorId', 'Netconf', 22, 420, 10, 5, NULL,
    '$hd_driver', '$plugging_driver')"
   mysql -e "use $db; $sql_statement"
else
   echo " Yes, it is."
fi


echo -n "Checking if 'Network_Node_template' is registered as hosting device template in $osn ..."
if [ "$plugin" == "n1kv" ]; then
   db="cisco_$osn"
else
   db="csr1kv_ovs_$osn"
fi
sql_statement="SELECT id FROM hostingdevicetemplates where id='11111110-2222-3333-4444-555555555555'"
hasTemplate=`mysql -e "use $db; $sql_statement" | awk '/id/ { print "Yes" }'`

if [ "$hasTemplate" != "Yes" ]; then
   echo " No, it is not. Registering it."

   # Columns: tenant_id, id, name, enabled, host_category, host_type, service_types, image, flavor, configurations_mechanism, transport_port, booting_time, slot_capacity, desired_slots_free, tenant_bound, device_driver, plugging_driver
   sql_statement="INSERT INTO hostingdevicetemplates VALUES
   ('$tenantId', '11111110-2222-3333-4444-555555555555',
    'Network_Node_template', TRUE, 'Hardware', 'NetworkNode', 'router:VPN:FW',
    NULL, NULL, 'CLI', NULL, NULL, 200, 0, NULL, '', '')"
   mysql -e "use $db; $sql_statement"
else
   echo " Yes, it is."
fi


echo -n "Checking if 'CSR1kv_router' is registered as router type in $osn ..."
sql_statement="SELECT id FROM routertypes where id='22221111-2222-3333-4444-555555555555'"
hasRouterType=`mysql -e "use $db; $sql_statement" | awk '/id/ { print "Yes" }'`

if [ "$hasRouterType" != "Yes" ]; then
   echo " No, it is not. Registering it."

   # Columns: id, name, description, template_id, slot_need, scheduler, cfg_agent_driver
   sql_statement="INSERT INTO routertypes VALUES
   ('22221111-2222-3333-4444-555555555555',
    'CSR1kv_router','Neutron Router implemented in Cisco CSR1kv',
    '11111111-2222-3333-4444-555555555555', 6,
    'neutron.plugins.cisco.l3.scheduler.l3_hosting_device_scheduler.L3RouterHostingDeviceScheduler',
    'neutron.plugins.cisco.l3.gent.csr1000v.cisco_csr_network_driver.CiscoCSRDriver')"
    mysql -e "use $db; $sql_statement"
else
   echo " Yes, it is."
fi


echo -n "Checking if 'Namespace_router' is registered as router type in $osn ..."
sql_statement="SELECT id FROM routertypes where id='22221112-2222-3333-4444-555555555555'"
hasRouterType=`mysql -e "use $db; $sql_statement" | awk '/id/ { print "Yes" }'`

if [ "$hasRouterType" != "Yes" ]; then
   echo " No, it is not. Registering it."

   # Columns: id, name, description, template_id, slot_need, scheduler, cfg_agent_driver
   sql_statement="INSERT INTO routertypes VALUES
   ('22221112-2222-3333-4444-555555555555', 'Namespace_router',
    'Neutron router implemented in Linux network namespace',
    '11111110-2222-3333-4444-555555555555', 6,
    '', '')"
   mysql -e "use $db; $sql_statement"
else
   echo " Yes, it is."
fi