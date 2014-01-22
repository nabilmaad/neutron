#!/bin/bash

# Default values
# --------------
plugin=${1:-n1kv}
#plugin=ovs

adminUser=quantum
l3AdminTenant=L3AdminTenant

vsmIP=192.168.168.2
vsmUsername=admin
vsmPassword=Sfish123

osnMgmtNwName=osn_mgmt_nw
mgmtSecGrp=mgmt_sec_grp
mgmtProviderNwName=mgmt_net
mgmtProviderVlanId=140
osnMgmtSubnetName=osn_mgmt_subnet
# note that the size of this network sets the limit on number of CSR instances
osnMgmtNw=10.0.100.0
osnMgmtNwLen=24
osnMgmtSubnet=$osnMgmtNw/$osnMgmtNwLen
# the first 9 addresses are set aside for L3CfgAgents and similar
osnMgmtRangeStart=10.0.100.10
osnMgmtRangeEnd=10.0.100.254

# Items in the arrays below correspond to settings for
# the Mgmt, T1 (i.e., VLAN) and T2 (i.e., VXLAN) networks/ports.
# the N1kv only supports one physical network so far
n1kvPhyNwNames=(osn_phy_network osn_phy_network osn_phy_network)
n1kvNwProfileNames=(osn_mgmt_np osn_t1_np osn_t2_np)
n1kvNwProfileTypes=(vlan trunk trunk)
#n1kvNwSubprofileTypes=(None vlan vxlan)
n1kvNwSubprofileTypes=(None vlan vlan)
n1kvNwProfileSegRange=($mgmtProviderVlanId-$mgmtProviderVlanId 500-2000 2001-3000)
n1kvPortPolicyProfileNames=(osn_mgmt_pp osn_t1_pp osn_t2_pp)

cisco=`quantum help | awk '/network-profile-create/ { if ($1 == "network-profile-create") { print "No"; } else { print "Yes"; }}'`
if [ "$cisco" == "Yes" ]; then
    CMD_NETWORK_PROFILE_LIST=cisco-network-profile-list
    CMD_NETWORK_PROFILE_CREATE=cisco-network-profile-create
    CMD_POLICY_PROFILE_LIST=cisco-policy-profile-list
else
    CMD_NETWORK_PROFILE_LIST=network-profile-list
    CMD_NETWORK_PROFILE_CREATE=network-profile-create
    CMD_POLICY_PROFILE_LIST=policy-profile-list
fi


function _configure_vsm_port_profiles() {
    # Package 'expect' must be installed for this function to work
    vsm_ip_addr=$1 user=$2 passwd=$3 profile_name=$4 expect -c '
	spawn /usr/bin/telnet $env(vsm_ip_addr)
	expect {
	    -re "Trying.*Connected.*Escape.*Nexus .*login: " {
	        send "$env(user)\n"
	        exp_continue
	        #look for the password prompt
	    }

	    "*?assword:*" {
	        send "$env(passwd)\n"
	    }
        }
        expect -re ".*# "

	send "config te\n"
	expect -re ".*# "

	send "feature network-segmentation-manager\n"
	expect -re ".*# "

    send "port-profile type vethernet $env(profile_name)\n"
	expect -re ".*# "

    send "no shut\n"
	expect -re ".*# "

    send "state enabled\n"
	expect -re ".*# "

    send "publish port-profile\n"
	expect -re ".*# "

    send "end\n"
    expect -re ".*# "

    send "exit\n"
    '
}


function get_network_profile_id() {
    name=$1
    phyNet=$2
    type=$3
    subType=$4
    segRange=$5
    local c=0
    local opt_param=

    nProfileId=`quantum $CMD_NETWORK_PROFILE_LIST | awk 'BEGIN { res="None"; } /'"$name"'/ { res=$2; } END { print res;}'`
    if [ "$nProfileId" == "None" ]; then
        echo "   Network profile $name does not exist. Creating it."
        if [ "$subType" != "None" ]; then
            opt_param="--sub_type $subType"
        fi
        if [ "$segRange" != "None" ]; then
            opt_param=$opt_param" --segment_range $segRange"
        fi
        quantum $CMD_NETWORK_PROFILE_CREATE --tenant-id $tenantId --physical_network $phyNet $opt_param $name $type
    fi
    while [ $c -le 5 ] && [ "$nProfileId" == "None" ]; do
        nProfileId=`quantum $CMD_NETWORK_PROFILE_LIST | awk 'BEGIN { res="None"; } /'"$name"'/ { res=$2; } END { print res;}'`
        let c+=1
    done
}


function get_port_profile_id() {
    name=$1
    local c=0
    pProfileId=`quantum $CMD_POLICY_PROFILE_LIST | awk 'BEGIN { res="None"; } /'"$name"'/ { res=$2; } END { print res;}'`
    if [ "$pProfileId" == "None" ]; then
        echo "   Port policy profile $name does not exist. Creating it."
        _configure_vsm_port_profiles $vsmIP $vsmUsername $vsmPassword $name
    fi
    while [ $c -le 5 ] && [ "$pProfileId" == "None" ]; do
        pProfileId=`quantum $CMD_POLICY_PROFILE_LIST | awk 'BEGIN { res="No"; } /'"$name"'/ { res=$2; } END { print res;}'`
        let c+=1
        sleep 1
    done
}


tenantId=`keystone tenant-get $l3AdminTenant 2>&1 | awk '/No tenant|id/ { if ($1 == "No") print "No"; else print $4; }'`
if [ "$tenantId" == "No" ]; then
    echo "No $l3AdminTenant exists, please create one using the setup_keystone... script then re-run this script."
    echo "Aborting!"
    exit 1
fi


source ~/devstack/openrc $adminUser $L3adminTenant


if [ "$plugin" == "n1kv" ]; then
    echo "Verifying that required N1kv network profiles exist:"
    for (( i=0; i<${#n1kvNwProfileNames[@]}; i++ )); do
        echo "   Checking ${n1kvNwProfileNames[$i]} ..."
        get_network_profile_id ${n1kvNwProfileNames[$i]} ${n1kvPhyNwNames[$i]} ${n1kvNwProfileTypes[$i]} ${n1kvNwSubprofileTypes[$i]} ${n1kvNwProfileSegRange[$i]}
        if [ $nProfileId == "None" ]; then
            echo "   Failed to verify network profile ${n1kvNwProfileNames[$i]}, please check health of the N1kv plugin and the VSM."
            echo "   Aborting!"
            exit 1
        else
            echo "   Done"
        fi
    done

    echo "Verifying that required N1kv port policy profiles exist:"
    for pn in ${n1kvPortPolicyProfileNames[@]}; do
        echo "   Checking $pn ..."
        get_port_profile_id $pn
        if [ $pProfileId == "None" ]; then
            echo "   Failed to verify port profile $pn, please check health of the VSM then re-run this script."
            echo "   Aborting!"
            exit 1
        else
            echo "   Done"
        fi
    done
fi


echo -n "Checking if $osnMgmtNwName network exists ..."
hasMgmtNetwork=`quantum net-show $osnMgmtNwName 2>&1 | awk '/Unable to find|enabled/ { if ($1 == "Unable") print "No"; else print "Yes"; }'`

if [ "$hasMgmtNetwork" == "No" ]; then
    echo " No, it does not. Creating it."
    if [ "$plugin" == "n1kv" ]; then
        get_network_profile_id ${n1kvNwProfileNames[0]} ${n1kvPhyNwNames[0]} ${n1kvNwProfileTypes[0]} ${n1kvNwSubprofileTypes[0]} ${n1kvNwProfileSegRange[0]}
        quantum net-create --tenant-id $tenantId $osnMgmtNwName --n1kv:profile_id $nProfileId
    else
        quantum net-create --tenant-id $tenantId $osnMgmtNwName --provider:network_type vlan --provider:physical_network pvnet1 --provider:segmentation_id $mgmtProviderVlanId
    fi
else
    echo " Yes, it does."
fi


echo -n "Checking if $osnMgmtSubnetName subnet exists ..."
hasMgmtSubnet=`quantum subnet-show $osnMgmtSubnetName 2>&1 | awk '/Unable to find|Value/ { if ($1 == "Unable") print "No"; else print "Yes"; }'`

if [ "$hasMgmtSubnet" == "No" ]; then
    echo " No, it does not. Creating it."
    quantum subnet-create --name $osnMgmtSubnetName --tenant-id $tenantId --allocation-pool start=$osnMgmtRangeStart,end=$osnMgmtRangeEnd $osnMgmtNwName $osnMgmtSubnet
else
    echo " Yes, it does."
fi


if [ "$plugin" == "n1kv" ]; then
    # security groups are not implemented by N1kv plugin so we stop here
    exit 0
fi


echo -n "Checking if $mgmtSecGrp security group exists ..."
hasMgmtSecGrp=`quantum security-group-show $mgmtSecGrp 2>&1 | awk '/Unable to find|Value/ { if ($1 == "Unable") print "No"; else print "Yes"; }'`

if [ "$hasMgmtSecGrp" == "No" ]; then
    echo " No, it does not. Creating it."
    quantum security-group-create --description "For CSR1kv management network" --tenant-id $tenantId $mgmtSecGrp
else
    echo " Yes, it does."
fi


proto="icmp"
echo -n "Checking if $mgmtSecGrp security group has $proto rule ..."
def=`quantum security-group-rule-list | awk -v grp=$mgmtSecGrp -v p=$proto  '/'"$proto"'|protocol/ { if ($4 == grp && $8 == p && $10 == "0.0.0.0/0") n++; } END { if (n > 0) print "Yes"; else print "No"; }'`
if [ "$def" == "No" ]; then
    echo " No, it does not. Creating it."
    quantum security-group-rule-create --tenant-id $tenantId --protocol icmp --remote-ip-prefix 0.0.0.0/0 $mgmtSecGrp
else
    echo " Yes, it does."
fi


proto="tcp"
echo -n "Checking if $mgmtSecGrp security group has $proto rule ..."
def=`quantum security-group-rule-list | awk -v grp=$mgmtSecGrp -v p=$proto '/'"$proto"'|protocol/ { if ($4 == grp && $8 == p && $10 == "0.0.0.0/0") n++; } END { if (n > 0) print "Yes"; else print "No"; }'`
if [ "$def" == "No" ]; then
    echo " No, it does not. Creating it."
    quantum security-group-rule-create --tenant-id $tenantId --protocol tcp --port-range-min 22 --port-range-max 22 --remote-ip-prefix 0.0.0.0/0 $mgmtSecGrp
else
    echo " Yes, it does."
fi
