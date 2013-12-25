# TODO(bgh): DIST_DIR and target for plugin
PHONY: help

AGENT_DIST_DIR=ovs_neutron_agent
AGENT_DIST_TARBALL=ovs_neutron_agent.tgz
VERSION=$(shell python -c "import sys ; sys.path.append('../../../neutron/') ; import version ; print version.canonical_version_string()")
XAPI_PLUGINS_DIR=$(AGENT_DIST_DIR)/build/ovs-neutron-agent-$(VERSION)/etc/xapi.d/plugins
RPM_BUILD_ROOT=$(AGENT_DIST_DIR)/build/rpm

help:
	@echo "make agent-dist-xen - to create the ovs-neutron-agent-${VERSION}-1.noarch.rpm"
	@echo "make agent-dist-xen-python26 - to create ovs-neutron-agent-${VERSION}-1.noarch.rpm and use python2.6"

agent-dist-xen:QUANTUM_LIBS=$(AGENT_DIST_DIR)/build/ovs-neutron-agent-$(VERSION)/usr/lib/python2.4/site-packages
agent-dist-xen-python26:QUANTUM_LIBS=$(AGENT_DIST_DIR)/build/ovs-neutron-agent-$(VERSION)/usr/lib/python2.6/site-packages

agent-dist-xen agent-dist-xen-python26: distclean
	yum --enablerepo=base install rpm-build
	mkdir -p $(XAPI_PLUGINS_DIR)
	mkdir -p $(QUANTUM_LIBS)/{neutron/plugins/openvswitch/common,neutron/openstack/common}
	mkdir -p $(QUANTUM_LIBS)/neutron/agent/linux
	mkdir -p $(RPM_BUILD_ROOT)/BUILD $(RPM_BUILD_ROOT)/SOURCES
	mkdir -p $(RPM_BUILD_ROOT)/SRPMS $(RPM_BUILD_ROOT)/RPMS $(RPM_BUILD_ROOT)/SPECS
	cp agent/*.py $(XAPI_PLUGINS_DIR)
	cp agent/*.sh $(AGENT_DIST_DIR)
	cp agent/ovs-neutron-agent-xs_xcp.spec $(AGENT_DIST_DIR)
	sed -i "s/VERSION/$(VERSION)/" $(AGENT_DIST_DIR)/ovs-neutron-agent-xs_xcp.spec
	cp README $(AGENT_DIST_DIR)
	cp ../../../etc/neutron/plugins/openvswitch/ovs_neutron_plugin.ini $(XAPI_PLUGINS_DIR)
	cp ../../agent/linux/{ovs_lib.py,utils.py,__init__.py} $(QUANTUM_LIBS)/neutron/agent/linux
	test -d $(AGENT_DIST_DIR)/build/ovs-neutron-agent-$(VERSION)/usr/lib/python2.6/site-packages && \
		sed -i 's/Requires:.*/Requires:	   python26 python26-sqlalchemy python26-mysqldb/' \
			 $(AGENT_DIST_DIR)/ovs-neutron-agent-xs_xcp.spec || true
	test -d $(AGENT_DIST_DIR)/build/ovs-neutron-agent-$(VERSION)/usr/lib/python2.6/site-packages && \
		sed -i 's/env python/env python2.6/' $(XAPI_PLUGINS_DIR)/ovs_neutron_agent.py || true
	cp ../../__init__.py $(QUANTUM_LIBS)/neutron/
	cp ../../agent/__init__.py $(QUANTUM_LIBS)/neutron/agent/
	cp ../__init__.py $(QUANTUM_LIBS)/neutron/plugins/
	cp __init__.py $(QUANTUM_LIBS)/neutron/plugins/openvswitch/
	cp common/{config.py,__init__.py} $(QUANTUM_LIBS)/neutron/plugins/openvswitch/common/
	cp ../../openstack/__init__.py $(QUANTUM_LIBS)/neutron/openstack/
	tar -czvpf ${RPM_BUILD_ROOT}/SOURCES/ovs-neutron-agent-${VERSION}.tgz -C $(AGENT_DIST_DIR)/build ovs-neutron-agent-${VERSION}
	rpmbuild -ba --define "_topdir $(shell pwd)/$(RPM_BUILD_ROOT)" --clean $(AGENT_DIST_DIR)/ovs-neutron-agent-xs_xcp.spec
	@echo "Agent package created: ovs_neutron_agent/build/rpm/RPMS/noarch/ovs-neutron-agent-${VERSION}-1.noarch.rpm"
	@echo "See README for installation details"

all:

clean:
	$(find . -name *.pyc | xargs rm)

distclean:
	-rm -rf $(AGENT_DIST_DIR)
	-rm -f $(AGENT_DIST_TARBALL)
