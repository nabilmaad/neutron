#!/bin/bash

function delete_service_resources_by_name() {
    service=$1
    resource=$2
    name=$3
    local list_command="list --field=id --field=name"
    local delete_command="delete"
    if [[ "$service" == "quantum" ]]; then
       list_command=$resource"-list --field=id --field=name"
       delete_command=$resource"-"$delete_command
    fi

#    echo $service $list_command
    ids=($($service $list_command | awk -v n=$name '$4 ~ n { print $2; }'))

    if [[ ${#ids[@]} > 0 ]]; then
       echo "Deleting ${#ids[@]} $resource resources named $name"
       for id in "${ids[@]}"; do
          echo "    $service $delete_command $id"
          $service $delete_command $id
       done
       if [[ "$service" == "nova" ]]; then
          wait_time=7
          echo "Waiting $wait_time seconds to let Nova clean up"
          sleep $wait_time
       fi
    else
       echo "No $resource resources named $name to delete"
    fi
}

source ~/devstack/openrc quantum L3AdminTenant

delete_service_resources_by_name nova server csr1kv_nrouter

delete_service_resources_by_name quantum port t1_p:
delete_service_resources_by_name quantum port t2_p:

delete_service_resources_by_name quantum subnet t1_sn:
delete_service_resources_by_name quantum subnet t2_sn:

delete_service_resources_by_name quantum net t1_n:
delete_service_resources_by_name quantum net t2_n:

source ~/devstack/localrc
table="cisco_quantum"
mysql -u$MYSQL_USER -p$MYSQL_PASSWORD -e "use $table; delete from hostingentities;"

echo
echo "Now please RESTART Neutron (Quantum) SERVER and L3 CFG AGENT!"
