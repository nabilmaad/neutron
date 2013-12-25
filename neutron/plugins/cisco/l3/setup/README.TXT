Run the scripts in this directory in the following order:

$ source [path to devstack directory]/openrc [ADMIN_USER_in_SOME_TENANT] [SOME_TENANT]
$ ./setup_keystone_for_csr1kv_l3.sh
$ ./setup_nova_and_glance_for_csrk1v.sh
$ ./setup_neutron_for_csr1kv.sh
$ ./setup_l3cfgagent_networking.sh
