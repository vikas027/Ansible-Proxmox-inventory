#!/usr/bin/env python

# Copyright (C) 2014  Mathieu GAUTHIER-LAFAYE <gauthierl@lapth.cnrs.fr>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# Updated 2016 by Matt Harris <matthaeus.harris@gmail.com>
#
# Added support for Proxmox VE 4.x
# Added support for using the Notes field of a VM to define groups and variables:
# A well-formatted JSON object in the Notes field will be added to the _meta
# section for that VM.  In addition, the "groups" key of this JSON object may be
# used to specify group membership:
#
# { "groups": ["utility", "databases"], "a": false, "b": true }

import urllib

try:
    import json
except ImportError:
    import simplejson as json
import os
import re
import sys
from optparse import OptionParser
from six import iteritems
from six.moves.urllib.error import HTTPError
from ansible.module_utils.urls import open_url


class ProxmoxNodeList(list):
    def get_names(self):
        return [node['node'] for node in self]


class ProxmoxVM(dict):
    def get_variables(self):
        variables = {}
        for key, value in iteritems(self):
            variables['proxmox_' + key] = value
        return variables


class ProxmoxVMList(list):
    def __init__(self, data=[], pxmxver=0.0):
        self.ver = pxmxver
        for item in data:
            self.append(ProxmoxVM(item))

    def get_names(self):
        if self.ver >= 4.0:
            return [vm['name'] for vm in self if vm['template'] != 1]
        else:
            return [vm['name'] for vm in self]

    def get_by_name(self, name):
        results = [vm for vm in self if vm['name'] == name]
        return results[0] if len(results) > 0 else None

    def get_variables(self):
        variables = {}
        for vm in self:
            variables[vm['name']] = vm.get_variables()

        return variables


class ProxmoxPoolList(list):
    def get_names(self):
        return [pool['poolid'] for pool in self]


class ProxmoxVersion(dict):
    def get_version(self):
        return float(self['version'])


class ProxmoxPool(dict):
    def get_members_name(self):
        return [member['name'] for member in self['members'] if member['template'] != 1]


class ProxmoxAPI(object):
    def __init__(self, options, config_path):
        self.options = options
        self.credentials = None

        if not options.url or not options.username or not options.password:
            if os.path.isfile(config_path):
                with open(config_path, "r") as config_file:
                    config_data = json.load(config_file)
                    if not options.url:
                        try:
                            options.url = config_data["url"]
                        except KeyError:
                            options.url = None
                    if not options.username:
                        try:
                            options.username = config_data["username"]
                        except KeyError:
                            options.username = None
                    if not options.password:
                        try:
                            options.password = config_data["password"]
                        except KeyError:
                            options.password = None

        if not options.url:
            raise Exception(
                'Missing mandatory parameter --url (or PROXMOX_URL or "url" key in config file).')
        elif not options.username:
            raise Exception(
                'Missing mandatory parameter --username (or PROXMOX_USERNAME or "username" key in config file).')
        elif not options.password:
            raise Exception(
                'Missing mandatory parameter --password (or PROXMOX_PASSWORD or "password" key in config file).')

    def auth(self):
        request_path = '{0}api2/json/access/ticket'.format(self.options.url)

        request_params = urllib.urlencode({
            'username': self.options.username,
            'password': self.options.password,
        })

        data = json.load(open_url(request_path, data=request_params,
                                  validate_certs=self.options.validate))

        self.credentials = {
            'ticket': data['data']['ticket'],
            'CSRFPreventionToken': data['data']['CSRFPreventionToken'],
        }

    def get(self, url, data=None):
        request_path = '{0}{1}'.format(self.options.url, url)

        headers = {'Cookie': 'PVEAuthCookie={0}'.format(
            self.credentials['ticket'])}
        request = open_url(request_path, data=data, headers=headers,
                           validate_certs=self.options.validate)

        response = json.load(request)
        return response['data']

    def nodes(self):
        return ProxmoxNodeList(self.get('api2/json/nodes'))

    def vms_by_type(self, node, type):
        return ProxmoxVMList(self.get('api2/json/nodes/{0}/{1}'.format(node, type)), self.version().get_version())

    def vm_config_by_type(self, node, vm, type):
        return self.get('api2/json/nodes/{0}/{1}/{2}/config'.format(node, type, vm))

    def node_qemu(self, node):
        return self.vms_by_type(node, 'qemu')

    def node_qemu_config(self, node, vm):
        return self.vm_config_by_type(node, vm, 'qemu')

    def node_qemu_agent_netifaces(self, node, vm):
        return self.get('api2/json/nodes/{0}/qemu/{1}/agent/network-get-interfaces'.format(node, vm))

    def node_qemu_agent_osinfo(self, node, vm):
        return self.get('api2/json/nodes/{0}/qemu/{1}/agent/get-osinfo'.format(node, vm))

    def node_lxc(self, node):
        return self.vms_by_type(node, 'lxc')

    def node_lxc_config(self, node, vm):
        return self.vm_config_by_type(node, vm, 'lxc')

    def node_openvz(self, node):
        return self.vms_by_type(node, 'openvz')

    def node_openvz_config(self, node, vm):
        return self.vm_config_by_type(node, vm, 'openvz')

    def pools(self):
        return ProxmoxPoolList(self.get('api2/json/pools'))

    def pool(self, poolid):
        return ProxmoxPool(self.get('api2/json/pools/{0}'.format(poolid)))

    def version(self):
        return ProxmoxVersion(self.get('api2/json/version'))


def main_list(options, config_path):
    results = {
        'all': {
            'hosts': [],
        },
        '_meta': {
            'hostvars': {},
        }
    }

    proxmox_api = ProxmoxAPI(options, config_path)
    proxmox_api.auth()

    for node in proxmox_api.nodes().get_names():
        try:
            qemu_list = proxmox_api.node_qemu(node)
        except HTTPError as error:
            # the API raises code 595 when target node is unavailable, skip it
            if error.code == 595:
                continue
            # if it was some other error, reraise it
            raise error
        results['all']['hosts'] += qemu_list.get_names()
        results['_meta']['hostvars'].update(qemu_list.get_variables())
        if proxmox_api.version().get_version() >= 4.0:
            lxc_list = proxmox_api.node_lxc(node)
            results['all']['hosts'] += lxc_list.get_names()
            results['_meta']['hostvars'].update(lxc_list.get_variables())
        else:
            openvz_list = proxmox_api.node_openvz(node)
            results['all']['hosts'] += openvz_list.get_names()
            results['_meta']['hostvars'].update(openvz_list.get_variables())

        # Merge QEMU and Containers lists from this node
        node_hostvars = qemu_list.get_variables().copy()
        if proxmox_api.version().get_version() >= 4.0:
            node_hostvars.update(lxc_list.get_variables())
        else:
            node_hostvars.update(openvz_list.get_variables())

        # Check only VM/containers from the current node
        for vm in node_hostvars:
            vmid = results['_meta']['hostvars'][vm]['proxmox_vmid']
            try:
                type = results['_meta']['hostvars'][vm]['proxmox_type']
            except KeyError:
                type = 'qemu'
            try:
                description = proxmox_api.vm_config_by_type(node, vmid, type)[
                    'description']
            except KeyError:
                description = None

            try:
                metadata = json.loads(description)
            except TypeError:
                metadata = {}
            except ValueError:
                metadata = {
                    'notes': description
                }

            if proxmox_api.version().get_version() >= 4.0:
                if type == 'lxc':
                    add_to_group("lxc", vm, results)

                    try:
                        net0 = proxmox_api.vm_config_by_type(
                            node, vmid, type)['net0']
                    except KeyError:
                        net0 = None

                    if net0:
                        ipMatch = re.search(
                            "(?<=ip=)(([0-9]{1,3}\.){3}[0-9]{1,3})", net0)
                        if ipMatch:
                            ansible_host = {
                                'ansible_host': ipMatch.group(1)
                            }
                            results['_meta']['hostvars'][vm].update(
                                ansible_host)

                    try:
                        osgroup = proxmox_api.vm_config_by_type(
                            node, vmid, type)['ostype']
                    except KeyError:
                        osgroup = None

                    if osgroup:
                        add_to_group("os_" + osgroup, vm, results)
                        add_to_subgroup("os_" + osgroup, "os_" +
                                        osgroup + "_lxc", vm, results)
                        add_to_subgroup("lxc", "lxc_" + osgroup, vm, results)

                if type == 'qemu':
                    add_to_group("qemu", vm, results)

                    try:
                        agent = proxmox_api.vm_config_by_type(
                            node, vmid, type)['agent']
                    except KeyError:
                        agent = None
                    try:
                        ide2 = proxmox_api.vm_config_by_type(
                            node, vmid, type)['ide2']
                    except KeyError:
                        ide2 = None

                    if ide2 and "cloudinit" in ide2:
                        try:
                            ipconfig0 = proxmox_api.vm_config_by_type(node, vmid, type)[
                                'ipconfig0']
                        except KeyError:
                            ipconfig0 = None

                        if ipconfig0:
                            ipMatch = re.search(
                                "(?<=ip=)(([0-9]{1,3}\.){3}[0-9]{1,3})", ipconfig0)
                            if ipMatch:
                                ansible_host = {
                                    'ansible_host': ipMatch.group(1)
                                }
                                results['_meta']['hostvars'][vm].update(
                                    ansible_host)
                    else:
                        if agent and agent == '1':
                            try:
                                ifaces = proxmox_api.node_qemu_agent_netifaces(
                                    node, vmid)
                            except:
                                ifaces = None

                            if ifaces:
                                for result in ifaces['result']:
                                    if result['name'] != 'lo' and result['name'] != 'docker0':
                                        for ipaddr in result['ip-addresses']:
                                            if ipaddr['ip-address-type'] == 'ipv4':
                                                ansible_host = {
                                                    'ansible_host': ipaddr['ip-address']
                                                }
                                                results['_meta']['hostvars'][vm].update(
                                                    ansible_host)
                        else:
                            add_to_group("no_auto_ip", vm, results)

                    if agent and agent == '1':
                        try:
                            osinfo = proxmox_api.node_qemu_agent_osinfo(node, vmid)[
                                'result']
                        except:
                            osinfo = None

                        if osinfo:
                            try:
                                osid = osinfo['id']
                            except KeyError:
                                osid = 'debian'

                            if osid:
                                add_to_group("os_" + osid, vm, results)
                                add_to_subgroup(
                                    "os_" + osid, "os_" + osid + "_qemu", vm, results)
                                add_to_subgroup(
                                    "qemu", "qemu_" + osid, vm, results)

            if 'groups' in metadata:
                # print metadata
                for group in metadata['groups']:
                    add_to_group(group, vm, results)

            # Create group 'running'
            # so you can: --limit 'running'
            # I am only showing running VMs, not containers
            status = results['_meta']['hostvars'][vm]['proxmox_status']
            is_lxc = results.get('_meta').get(
                'hostvars').get(vm).get('proxmox_type')
            if status == 'running' and is_lxc != 'lxc':
                if 'running' not in results:
                    results['running'] = {
                        'hosts': []
                    }
                results['running']['hosts'] += [vm]

            results['_meta']['hostvars'][vm].update(metadata)

    # pools
    for pool in proxmox_api.pools().get_names():
        results[pool] = {
            'hosts': proxmox_api.pool(pool).get_members_name(),
        }

    return results


def add_to_group(groupname, vm, results):
    if groupname not in results:
        results[groupname] = {
            'hosts': [],
            'children': []
        }
    results[groupname]['hosts'] += [vm]


def add_to_subgroup(groupname, subgroupname, vm, results):
    if groupname not in results:
        results[groupname] = {
            'hosts': [],
            'children': []
        }
    if subgroupname not in results[groupname]['children']:
        results[groupname]['children'] += [subgroupname]
    if subgroupname not in results:
        results[subgroupname] = {
            'hosts': [],
            'children': []
        }
    results[subgroupname]['hosts'] += [vm]


def main_host(options, config_path):
    proxmox_api = ProxmoxAPI(options, config_path)
    proxmox_api.auth()

    for node in proxmox_api.nodes().get_names():
        qemu_list = proxmox_api.node_qemu(node)
        qemu = qemu_list.get_by_name(options.host)
        if qemu:
            return qemu.get_variables()

    return {}


def main():
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        os.path.splitext(os.path.basename(__file__))[0] + ".json"
    )

    bool_validate_cert = True
    if os.path.isfile(config_path):
        with open(config_path, "r") as config_file:
            config_data = json.load(config_file)
            try:
                bool_validate_cert = config_data["validateCert"]
            except KeyError:
                pass
    if os.environ.has_key('PROXMOX_INVALID_CERT'):
        bool_validate_cert = False

    parser = OptionParser(usage='%prog [options] --list | --host HOSTNAME')
    parser.add_option('--list', action="store_true",
                      default=False, dest="list")
    parser.add_option('--host', dest="host")
    parser.add_option(
        '--url', default=os.environ.get('PROXMOX_URL'), dest='url')
    parser.add_option(
        '--username', default=os.environ.get('PROXMOX_USERNAME'), dest='username')
    parser.add_option(
        '--password', default=os.environ.get('PROXMOX_PASSWORD'), dest='password')
    parser.add_option('--pretty', action="store_true",
                      default=True, dest='pretty')
    parser.add_option('--show-lxc', action="store_true",
                      default=False, dest='showlxc')
    parser.add_option('--trust-invalid-certs', action="store_false",
                      default=bool_validate_cert, dest='validate')
    (options, args) = parser.parse_args()

    if options.list:
        data = main_list(options, config_path)
    elif options.host:
        data = main_host(options, config_path)
    else:
        parser.print_help()
        sys.exit(1)

    indent = None
    if options.pretty:
        indent = 2

    print(json.dumps(data, indent=indent))


if __name__ == '__main__':
    main()
