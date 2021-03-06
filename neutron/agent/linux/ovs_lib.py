# Copyright 2011 VMware, Inc.
# All Rights Reserved.
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

import itertools
import numbers
import operator

from oslo.config import cfg
from oslo.serialization import jsonutils
from oslo.utils import excutils
import retrying
import six

from neutron.agent.linux import ip_lib
from neutron.agent.linux import utils
from neutron.common import exceptions
from neutron.i18n import _LE, _LI, _LW
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants

# Default timeout for ovs-vsctl command
DEFAULT_OVS_VSCTL_TIMEOUT = 10

# Special return value for an invalid OVS ofport
INVALID_OFPORT = '-1'

OPTS = [
    cfg.IntOpt('ovs_vsctl_timeout',
               default=DEFAULT_OVS_VSCTL_TIMEOUT,
               help=_('Timeout in seconds for ovs-vsctl commands')),
]
cfg.CONF.register_opts(OPTS)

LOG = logging.getLogger(__name__)


def _ofport_result_pending(result):
    """Return True if ovs-vsctl indicates the result is still pending."""
    # ovs-vsctl can return '[]' for an ofport that has not yet been assigned
    try:
        int(result)
        return False
    except (ValueError, TypeError):
        return True


def _ofport_retry(fn):
    """Decorator for retrying when OVS has yet to assign an ofport.

    The instance's vsctl_timeout is used as the max waiting time. This relies
    on the fact that instance methods receive self as the first argument.
    """
    @six.wraps(fn)
    def wrapped(*args, **kwargs):
        self = args[0]
        new_fn = retrying.retry(
            retry_on_result=_ofport_result_pending,
            stop_max_delay=self.vsctl_timeout * 1000,
            wait_exponential_multiplier=10,
            wait_exponential_max=1000,
            retry_on_exception=lambda _: False)(fn)
        return new_fn(*args, **kwargs)
    return wrapped


class VifPort:
    def __init__(self, port_name, ofport, vif_id, vif_mac, switch):
        self.port_name = port_name
        self.ofport = ofport
        self.vif_id = vif_id
        self.vif_mac = vif_mac
        self.switch = switch

    def __str__(self):
        return ("iface-id=" + self.vif_id + ", vif_mac=" +
                self.vif_mac + ", port_name=" + self.port_name +
                ", ofport=" + str(self.ofport) + ", bridge_name=" +
                self.switch.br_name)


class BaseOVS(object):

    def __init__(self, root_helper):
        self.root_helper = root_helper
        self.vsctl_timeout = cfg.CONF.ovs_vsctl_timeout

    def run_vsctl(self, args, check_error=False):
        full_args = ["ovs-vsctl", "--timeout=%d" % self.vsctl_timeout] + args
        try:
            return utils.execute(full_args, root_helper=self.root_helper)
        except Exception as e:
            with excutils.save_and_reraise_exception() as ctxt:
                LOG.error(_LE("Unable to execute %(cmd)s. "
                              "Exception: %(exception)s"),
                          {'cmd': full_args, 'exception': e})
                if not check_error:
                    ctxt.reraise = False

    def add_bridge(self, bridge_name):
        self.run_vsctl(["--", "--may-exist", "add-br", bridge_name])
        return OVSBridge(bridge_name, self.root_helper)

    def delete_bridge(self, bridge_name):
        self.run_vsctl(["--", "--if-exists", "del-br", bridge_name])

    def bridge_exists(self, bridge_name):
        try:
            self.run_vsctl(['br-exists', bridge_name], check_error=True)
        except RuntimeError as e:
            with excutils.save_and_reraise_exception() as ctxt:
                if 'Exit code: 2\n' in str(e):
                    ctxt.reraise = False
                    return False
        return True

    def get_bridge_name_for_port_name(self, port_name):
        try:
            return self.run_vsctl(['port-to-br', port_name], check_error=True)
        except RuntimeError as e:
            with excutils.save_and_reraise_exception() as ctxt:
                if 'Exit code: 1\n' in str(e):
                    ctxt.reraise = False

    def port_exists(self, port_name):
        return bool(self.get_bridge_name_for_port_name(port_name))


class OVSBridge(BaseOVS):
    def __init__(self, br_name, root_helper):
        super(OVSBridge, self).__init__(root_helper)
        self.br_name = br_name

    def set_controller(self, controller_names):
        vsctl_command = ['--', 'set-controller', self.br_name]
        vsctl_command.extend(controller_names)
        self.run_vsctl(vsctl_command, check_error=True)

    def del_controller(self):
        self.run_vsctl(['--', 'del-controller', self.br_name],
                       check_error=True)

    def get_controller(self):
        res = self.run_vsctl(['--', 'get-controller', self.br_name],
                             check_error=True)
        if res:
            return res.strip().split('\n')
        return res

    def set_secure_mode(self):
        self.run_vsctl(['--', 'set-fail-mode', self.br_name, 'secure'],
                       check_error=True)

    def set_protocols(self, protocols):
        self.run_vsctl(['--', 'set', 'bridge', self.br_name,
                        "protocols=%s" % protocols],
                       check_error=True)

    def create(self):
        self.add_bridge(self.br_name)

    def destroy(self):
        self.delete_bridge(self.br_name)

    def reset_bridge(self):
        self.destroy()
        self.create()

    def add_port(self, port_name, *interface_options):
        args = ["--", "--may-exist", "add-port", self.br_name, port_name]
        if interface_options:
            args += ['--', 'set', 'Interface', port_name]
            args += ['%s=%s' % kv for kv in interface_options]
        self.run_vsctl(args)
        ofport = self.get_port_ofport(port_name)
        if ofport == INVALID_OFPORT:
            self.delete_port(port_name)
        return ofport

    def replace_port(self, port_name, *interface_attr_tuples):
        """Replace existing port or create it, and configure port interface."""
        cmd = ['--', '--if-exists', 'del-port', port_name,
               '--', 'add-port', self.br_name, port_name]
        if interface_attr_tuples:
            cmd += ['--', 'set', 'Interface', port_name]
            cmd += ['%s=%s' % kv for kv in interface_attr_tuples]
        self.run_vsctl(cmd)

    def delete_port(self, port_name):
        self.run_vsctl(["--", "--if-exists", "del-port", self.br_name,
                        port_name])

    def set_db_attribute(self, table_name, record, column, value):
        args = ["set", table_name, record, "%s=%s" % (column, value)]
        self.run_vsctl(args)

    def clear_db_attribute(self, table_name, record, column):
        args = ["clear", table_name, record, column]
        self.run_vsctl(args)

    def run_ofctl(self, cmd, args, process_input=None):
        full_args = ["ovs-ofctl", cmd, self.br_name] + args
        try:
            return utils.execute(full_args, root_helper=self.root_helper,
                                 process_input=process_input)
        except Exception as e:
            LOG.error(_LE("Unable to execute %(cmd)s. Exception: "
                          "%(exception)s"),
                      {'cmd': full_args, 'exception': e})

    def count_flows(self):
        flow_list = self.run_ofctl("dump-flows", []).split("\n")[1:]
        return len(flow_list) - 1

    def remove_all_flows(self):
        self.run_ofctl("del-flows", [])

    @_ofport_retry
    def _get_port_ofport(self, port_name):
        return self.db_get_val("Interface", port_name, "ofport")

    def get_port_ofport(self, port_name):
        """Get the port's assigned ofport, retrying if not yet assigned."""
        ofport = INVALID_OFPORT
        try:
            ofport = self._get_port_ofport(port_name)
        except retrying.RetryError as e:
            LOG.exception(_LE("Timed out retrieving ofport on port %(pname)s. "
                              "Exception: %(exception)s"),
                          {'pname': port_name, 'exception': e})
        return ofport

    def get_datapath_id(self):
        return self.db_get_val('Bridge',
                               self.br_name, 'datapath_id').strip('"')

    def do_action_flows(self, action, kwargs_list):
        flow_strs = [_build_flow_expr_str(kw, action) for kw in kwargs_list]
        self.run_ofctl('%s-flows' % action, ['-'], '\n'.join(flow_strs))

    def add_flow(self, **kwargs):
        self.do_action_flows('add', [kwargs])

    def mod_flow(self, **kwargs):
        self.do_action_flows('mod', [kwargs])

    def delete_flows(self, **kwargs):
        self.do_action_flows('del', [kwargs])

    def dump_flows_for_table(self, table):
        retval = None
        flow_str = "table=%s" % table
        flows = self.run_ofctl("dump-flows", [flow_str])
        if flows:
            retval = '\n'.join(item for item in flows.splitlines()
                               if 'NXST' not in item)
        return retval

    def deferred(self, **kwargs):
        return DeferredOVSBridge(self, **kwargs)

    def add_tunnel_port(self, port_name, remote_ip, local_ip,
                        tunnel_type=constants.TYPE_GRE,
                        vxlan_udp_port=constants.VXLAN_UDP_PORT,
                        dont_fragment=True):
        options = [('type', tunnel_type)]
        if (tunnel_type == constants.TYPE_VXLAN and
                vxlan_udp_port != constants.VXLAN_UDP_PORT):
            # Only set the VXLAN UDP port if it's not the default
            options.append(("options:dst_port", vxlan_udp_port))
        options.extend([
            ("options:df_default", str(bool(dont_fragment)).lower()),
            ("options:remote_ip", remote_ip),
            ("options:local_ip", local_ip),
            ("options:in_key", "flow"),
            ("options:out_key", "flow")])
        return self.add_port(port_name, *options)

    def add_patch_port(self, local_name, remote_name):
        options = [
            ('type', 'patch'),
            ('options:peer', remote_name)
        ]
        return self.add_port(local_name, *options)

    def db_get_map(self, table, record, column, check_error=False):
        output = self.run_vsctl(["get", table, record, column], check_error)
        if output:
            output_str = output.rstrip("\n\r")
            return self.db_str_to_map(output_str)
        return {}

    def db_get_val(self, table, record, column, check_error=False):
        output = self.run_vsctl(["get", table, record, column], check_error)
        if output:
            return output.rstrip("\n\r")

    def db_str_to_map(self, full_str):
        list = full_str.strip("{}").split(", ")
        ret = {}
        for e in list:
            if e.find("=") == -1:
                continue
            arr = e.split("=")
            ret[arr[0]] = arr[1].strip("\"")
        return ret

    def get_port_name_list(self):
        res = self.run_vsctl(["list-ports", self.br_name], check_error=True)
        if res:
            return res.strip().split("\n")
        return []

    def get_port_stats(self, port_name):
        return self.db_get_map("Interface", port_name, "statistics")

    def get_xapi_iface_id(self, xs_vif_uuid):
        args = ["xe", "vif-param-get", "param-name=other-config",
                "param-key=nicira-iface-id", "uuid=%s" % xs_vif_uuid]
        try:
            return utils.execute(args, root_helper=self.root_helper).strip()
        except Exception as e:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE("Unable to execute %(cmd)s. "
                              "Exception: %(exception)s"),
                          {'cmd': args, 'exception': e})

    # returns a VIF object for each VIF port
    def get_vif_ports(self):
        edge_ports = []
        port_names = self.get_port_name_list()
        for name in port_names:
            external_ids = self.db_get_map("Interface", name, "external_ids",
                                           check_error=True)
            ofport = self.db_get_val("Interface", name, "ofport",
                                     check_error=True)
            if "iface-id" in external_ids and "attached-mac" in external_ids:
                p = VifPort(name, ofport, external_ids["iface-id"],
                            external_ids["attached-mac"], self)
                edge_ports.append(p)
            elif ("xs-vif-uuid" in external_ids and
                  "attached-mac" in external_ids):
                # if this is a xenserver and iface-id is not automatically
                # synced to OVS from XAPI, we grab it from XAPI directly
                iface_id = self.get_xapi_iface_id(external_ids["xs-vif-uuid"])
                p = VifPort(name, ofport, iface_id,
                            external_ids["attached-mac"], self)
                edge_ports.append(p)

        return edge_ports

    def get_vif_port_set(self):
        edge_ports = set()
        args = ['--format=json', '--', '--columns=external_ids,ofport',
                'list', 'Interface'] + self.get_port_name_list()
        result = self.run_vsctl(args, check_error=True)
        if not result:
            return edge_ports
        for row in jsonutils.loads(result)['data']:
            external_ids = dict(row[0][1])
            # Do not consider VIFs which aren't yet ready
            # This can happen when ofport values are either [] or ["set", []]
            # We will therefore consider only integer values for ofport
            ofport = row[1]
            if not isinstance(ofport, numbers.Integral):
                LOG.warn(_LW("Found not yet ready openvswitch port: %s"), row)
            elif ofport < 1:
                LOG.warn(_LW("Found failed openvswitch port: %s"), row)
            elif 'attached-mac' in external_ids:
                if "iface-id" in external_ids:
                    edge_ports.add(external_ids['iface-id'])
                elif "xs-vif-uuid" in external_ids:
                    # if this is a xenserver and iface-id is not
                    # automatically synced to OVS from XAPI, we grab it
                    # from XAPI directly
                    iface_id = self.get_xapi_iface_id(
                        external_ids["xs-vif-uuid"])
                    edge_ports.add(iface_id)
        return edge_ports

    def get_port_tag_dict(self):
        """Get a dict of port names and associated vlan tags.

        e.g. the returned dict is of the following form::

            {u'int-br-eth2': [],
             u'patch-tun': [],
             u'qr-76d9e6b6-21': 1,
             u'tapce5318ff-78': 1,
             u'tape1400310-e6': 1}

        The TAG ID is only available in the "Port" table and is not available
        in the "Interface" table queried by the get_vif_port_set() method.

        """
        args = ['--format=json', '--', '--columns=name,tag', 'list', 'Port']
        args += self.get_port_name_list()
        result = self.run_vsctl(args, check_error=True)
        port_tag_dict = {}
        if not result:
            return port_tag_dict
        for name, tag in jsonutils.loads(result)['data']:
            # 'tag' can be [u'set', []] or an integer
            if isinstance(tag, list):
                tag = tag[1]
            port_tag_dict[name] = tag
        return port_tag_dict

    def get_vif_port_by_id(self, port_id):
        args = ['--format=json', '--', '--columns=external_ids,name,ofport',
                'find', 'Interface',
                'external_ids:iface-id="%s"' % port_id]
        result = self.run_vsctl(args)
        if not result:
            return
        json_result = jsonutils.loads(result)
        try:
            # Retrieve the indexes of the columns we're looking for
            headings = json_result['headings']
            ext_ids_idx = headings.index('external_ids')
            name_idx = headings.index('name')
            ofport_idx = headings.index('ofport')
            # If data attribute is missing or empty the line below will raise
            # an exeception which will be captured in this block.
            # We won't deal with the possibility of ovs-vsctl return multiple
            # rows since the interface identifier is unique
            for data in json_result['data']:
                port_name = data[name_idx]
                switch = get_bridge_for_iface(self.root_helper, port_name)
                if switch != self.br_name:
                    continue
                ofport = data[ofport_idx]
                # ofport must be integer otherwise return None
                if not isinstance(ofport, int) or ofport == -1:
                    LOG.warn(_LW("ofport: %(ofport)s for VIF: %(vif)s is not a"
                                 " positive integer"), {'ofport': ofport,
                                                        'vif': port_id})
                    return
                # Find VIF's mac address in external ids
                ext_id_dict = dict((item[0], item[1]) for item in
                                   data[ext_ids_idx][1])
                vif_mac = ext_id_dict['attached-mac']
                return VifPort(port_name, ofport, port_id, vif_mac, self)
            LOG.info(_LI("Port %(port_id)s not present in bridge %(br_name)s"),
                     {'port_id': port_id, 'br_name': self.br_name})
        except Exception as error:
            LOG.warn(_LW("Unable to parse interface details. Exception: %s"),
                     error)
            return

    def delete_ports(self, all_ports=False):
        if all_ports:
            port_names = self.get_port_name_list()
        else:
            port_names = (port.port_name for port in self.get_vif_ports())

        for port_name in port_names:
            self.delete_port(port_name)

    def get_local_port_mac(self):
        """Retrieve the mac of the bridge's local port."""
        address = ip_lib.IPDevice(self.br_name, self.root_helper).link.address
        if address:
            return address
        else:
            msg = _('Unable to determine mac address for %s') % self.br_name
            raise Exception(msg)

    def __enter__(self):
        self.create()
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.destroy()


class DeferredOVSBridge(object):
    '''Deferred OVSBridge.

    This class wraps add_flow, mod_flow and delete_flows calls to an OVSBridge
    and defers their application until apply_flows call in order to perform
    bulk calls. It wraps also ALLOWED_PASSTHROUGHS calls to avoid mixing
    OVSBridge and DeferredOVSBridge uses.
    This class can be used as a context, in such case apply_flows is called on
    __exit__ except if an exception is raised.
    This class is not thread-safe, that's why for every use a new instance
    must be implemented.
    '''
    ALLOWED_PASSTHROUGHS = 'add_port', 'add_tunnel_port', 'delete_port'

    def __init__(self, br, full_ordered=False,
                 order=('add', 'mod', 'del')):
        '''Constructor.

        :param br: wrapped bridge
        :param full_ordered: Optional, disable flow reordering (slower)
        :param order: Optional, define in which order flow are applied
        '''

        self.br = br
        self.full_ordered = full_ordered
        self.order = order
        if not self.full_ordered:
            self.weights = dict((y, x) for x, y in enumerate(self.order))
        self.action_flow_tuples = []

    def __getattr__(self, name):
        if name in self.ALLOWED_PASSTHROUGHS:
            return getattr(self.br, name)
        raise AttributeError(name)

    def add_flow(self, **kwargs):
        self.action_flow_tuples.append(('add', kwargs))

    def mod_flow(self, **kwargs):
        self.action_flow_tuples.append(('mod', kwargs))

    def delete_flows(self, **kwargs):
        self.action_flow_tuples.append(('del', kwargs))

    def apply_flows(self):
        action_flow_tuples = self.action_flow_tuples
        self.action_flow_tuples = []
        if not action_flow_tuples:
            return

        if not self.full_ordered:
            action_flow_tuples.sort(key=lambda af: self.weights[af[0]])

        grouped = itertools.groupby(action_flow_tuples,
                                    key=operator.itemgetter(0))
        itemgetter_1 = operator.itemgetter(1)
        for action, action_flow_list in grouped:
            flows = map(itemgetter_1, action_flow_list)
            self.br.do_action_flows(action, flows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.apply_flows()
        else:
            LOG.exception(_LE("OVS flows could not be applied on bridge %s"),
                          self.br.br_name)


def get_bridge_for_iface(root_helper, iface):
    args = ["ovs-vsctl", "--timeout=%d" % cfg.CONF.ovs_vsctl_timeout,
            "iface-to-br", iface]
    try:
        return utils.execute(args, root_helper=root_helper).strip()
    except Exception:
        LOG.exception(_LE("Interface %s not found."), iface)
        return None


def get_bridges(root_helper):
    args = ["ovs-vsctl", "--timeout=%d" % cfg.CONF.ovs_vsctl_timeout,
            "list-br"]
    try:
        return utils.execute(args, root_helper=root_helper).strip().split("\n")
    except Exception as e:
        with excutils.save_and_reraise_exception():
            LOG.exception(_LE("Unable to retrieve bridges. Exception: %s"), e)


def get_bridge_external_bridge_id(root_helper, bridge):
    args = ["ovs-vsctl", "--timeout=2", "br-get-external-id",
            bridge, "bridge-id"]
    try:
        return utils.execute(args, root_helper=root_helper).strip()
    except Exception:
        LOG.exception(_LE("Bridge %s not found."), bridge)
        return None


def _build_flow_expr_str(flow_dict, cmd):
    flow_expr_arr = []
    actions = None

    if cmd == 'add':
        flow_expr_arr.append("hard_timeout=%s" %
                             flow_dict.pop('hard_timeout', '0'))
        flow_expr_arr.append("idle_timeout=%s" %
                             flow_dict.pop('idle_timeout', '0'))
        flow_expr_arr.append("priority=%s" %
                             flow_dict.pop('priority', '1'))
    elif 'priority' in flow_dict:
        msg = _("Cannot match priority on flow deletion or modification")
        raise exceptions.InvalidInput(error_message=msg)

    if cmd != 'del':
        if "actions" not in flow_dict:
            msg = _("Must specify one or more actions on flow addition"
                    " or modification")
            raise exceptions.InvalidInput(error_message=msg)
        actions = "actions=%s" % flow_dict.pop('actions')

    for key, value in flow_dict.iteritems():
        if key == 'proto':
            flow_expr_arr.append(value)
        else:
            flow_expr_arr.append("%s=%s" % (key, str(value)))

    if actions:
        flow_expr_arr.append(actions)

    return ','.join(flow_expr_arr)
