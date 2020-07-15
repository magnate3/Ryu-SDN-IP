import json
from netaddr import IPNetwork
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_0, ofproto_v1_3
from ryu.lib import hub
from ryu.lib import ofctl_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ipv4
from ryu.lib.packet import ether_types
from ryu.topology import api as topo_api
from ryu.services.protocols.bgp.bgpspeaker import BGPSpeaker
from ryu.lib.ofp_pktinfilter import packet_in_filter, RequiredTypeFilter
from .conf_mgr import SDNIPConfigManager
from .fwd import Fwd
from .hop_db import HopDB

# integrate with DragonKnight CLI
with_dk = False
try:
    from dragon_knight import dk_plugin
    with_dk = True
except ImportError as e:
    pass


class SDNIP(app_manager.RyuApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {
        'fwd': Fwd,
        'hop_db': HopDB
    }

    def __init__(self, *args, **kwargs):
        super(SDNIP, self).__init__(*args, **kwargs)
        self.fwd = kwargs['fwd']
        self.hop_db = kwargs['hop_db']
        self.cfg_mgr = SDNIPConfigManager()
        self.waiters = {}
        self.bgp_speaker =\
            BGPSpeaker(self.cfg_mgr.as_number,
                       str(self.cfg_mgr.router_id),
                       bgp_server_port=self.cfg_mgr.listen_port,
                       best_path_change_handler=self.best_path_change_handler,
                       peer_down_handler=self.peer_down_handler,
                       peer_up_handler=self.peer_up_handler)

        speaker_ids = self.cfg_mgr.get_all_speaker_id()

        for speaker_id in speaker_ids:
            self.bgp_speaker.neighbor_add(speaker_id,
                                          self.cfg_mgr.as_number,
                                          is_next_hop_self=True)

        hub.spawn(self.prefix_check_loop)

        if with_dk:
            dk_plugin.DynamicLoader.register_custom_cmd('sdn-ip:info', self.cmd_self_info)
            dk_plugin.DynamicLoader.register_custom_cmd('sdn-ip:routes', self.cmd_list_routes)
            dk_plugin.DynamicLoader.register_custom_cmd('sdn-ip:flows', self.cmd_get_flows)

    def best_path_change_handler(self, ev):
        self.logger.info('best path changed:')
        self.logger.info('remote_as: %d', ev.remote_as)
        self.logger.info('route_dist: %s', ev.route_dist)
        self.logger.info('prefix: %s', ev.prefix)
        self.logger.info('nexthop: %s', ev.nexthop)
        self.logger.info('label: %s', ev.label)
        self.logger.info('is_withdraw: %s', ev.is_withdraw)
        self.logger.info('')

        # Ignore internal network
        prefix_nw = IPNetwork(ev.prefix)

        for internal_network in self.cfg_mgr.get_internal_networks():
            int_nw = IPNetwork(internal_network)

            if int_nw == prefix_nw:
                self.logger.info('Internal network, ignored.')
                return

        if ev.is_withdraw:
            self.hop_db.withdraw(ev.prefix)
            self.uninstall_best_path(ev.prefix, ev.nexthop)

        else:
            self.hop_db.add_hop(ev.prefix, ev.nexthop)
            self.install_best_path(ev.prefix, ev.nexthop)

    def peer_down_handler(self, remote_ip, remote_as):
        self.logger.info('peer down:')
        self.logger.info('remote_as: %d', remote_as)
        self.logger.info('remote ip: %s', remote_ip)
        self.logger.info('')

    def peer_up_handler(self, remote_ip, remote_as):
        self.logger.info('peer up:')
        self.logger.info('remote_as: %d', remote_as)
        self.logger.info('remote ip: %s', remote_ip)
        self.logger.info('')

    def get_host(self, ip):
        hosts = topo_api.get_all_host(self)

        for host in hosts:
            if ip in host.ipv4:
                return host

        return None

    def prefix_check_loop(self):

        while True:
            prefixs_to_install = self.hop_db.get_uninstalled_prefix_list()
            self.logger.debug("prefix to install: %s", str(prefixs_to_install))

            for prefix in prefixs_to_install:
                nexthop = self.hop_db.get_nexthop(prefix)
                self.install_best_path(prefix, nexthop)

            hub.sleep(3)

    def install_best_path(self, prefix, nexthop):

        nexthop_host = self.get_host(nexthop)
        self.logger.debug("nexthop host: %s", str(nexthop_host))
        if nexthop_host is None:
            self.logger.debug("Can't find nexthop host: %s", str(nexthop_host))
            return

        nexthop_port = nexthop_host.port
        nexthop_mac = nexthop_host.mac
        nexthop_dpid = nexthop_port.dpid
        nexthop_port_no = nexthop_port.port_no
        prefix_ip = str(IPNetwork(prefix).ip)
        prefix_mask = str(IPNetwork(prefix).netmask)

        for dp in self.fwd.get_all_datapaths():
            from_dpid = dp.id
            nexthop_match =\
                dp.ofproto_parser.OFPMatch(ipv4_dst=(prefix_ip, prefix_mask),
                                           eth_type=2048)
            pre_actions = [
                dp.ofproto_parser.OFPActionSetField(eth_dst=nexthop_mac)
                ]

            self.fwd.setup_shortest_path(from_dpid,
                                         nexthop_dpid,
                                         nexthop_port_no,
                                         nexthop_match,
                                         pre_actions)

        self.hop_db.install_prefix(prefix)

    def uninstall_best_path(self, prefix, nexthop):

        prefix_ip = str(IPNetwork(prefix).ip)
        prefix_mask = str(IPNetwork(prefix).netmask)

        # remove all flow rule for this prefix
        for dp in self.fwd.get_all_datapaths():
            match =\
                dp.ofproto_parser.OFPMatch(ipv4_dst=(prefix_ip, prefix_mask),
                                       eth_type=2048)
            flow_del = dp.ofproto_parser.OFPFlowMod(datapath=dp,
                                    command=dp.ofproto.OFPFC_DELETE,
                                    match=match)
            dp.send_msg(flow_del)

    def install_internal_host_path(self, ip):
        host = self.get_host(ip)

        if host is None:
            return

        for dp in self.fwd.get_all_datapaths():
            from_dpid = dp.id
            host_match =\
                dp.ofproto_parser.OFPMatch(ipv4_dst=ip, eth_type=2048)
            pre_actions = [
                dp.ofproto_parser.OFPActionSetField(eth_dst=host.mac)
                ]

            self.fwd.setup_shortest_path(from_dpid,
                                         host.port.dpid,
                                         host.port.port_no,
                                         host_match,
                                         pre_actions)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    @packet_in_filter(RequiredTypeFilter, {'types': [ipv4.ipv4]})
    def internal_host_route_handler(self, ev):
        '''
        Handle internal network host routing
        '''
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        ofproto = dp.ofproto

        pkt = packet.Packet(msg.data)
        ipv4_header = pkt.get_protocol(ipv4.ipv4)

        src_ip = ipv4_header.src
        dst_ip = ipv4_header.dst

        if not self.cfg_mgr.is_internal_host(dst_ip):
            return

        self.install_internal_host_path(dst_ip)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_status_reply_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath

        if dp.id not in self.waiters:
            return
        if msg.xid not in self.waiters[dp.id]:
            return
        lock, msgs = self.waiters[dp.id][msg.xid]
        msgs.append(msg)

        flags = 0
        flags = dp.ofproto.OFPMPF_REPLY_MORE

        if msg.flags & flags:
            return
        del self.waiters[dp.id][msg.xid]
        lock.set()

    # commands
    def cmd_self_info(self):
        information = "AS number : {}\n" + \
                      "Router ID: {}\n" + \
                      "BGP port: {}\n"

        information = information.format(self.cfg_mgr.as_number,
                                         str(self.cfg_mgr.router_id),
                                         self.cfg_mgr.listen_port)

        return information

    def cmd_list_routes(self):
        prefix_list = self.hop_db.get_all_prefixes()
        result = "{:<20}{:<17}{}\n".format("Prefix", "Next Hop", "Path installed")

        result = result + "=" * 40 + "\n"

        for prefix in prefix_list:
            result = result + "{:<20}".format(prefix)
            _next_hop = self.hop_db.hops.get(prefix)

            if _next_hop is None:
                _next_hop = "None"

            result = result + "{:<17}".format(_next_hop)
            result = result + str(self.hop_db.is_prefix_installed(prefix))
            result = result + "\n"

        return result

    def cmd_get_flows(self):
        result = ""
        for dp in self.fwd.get_all_datapaths():
            flows = ofctl_v1_3.get_flow_stats(dp, self.waiters, {})

            result = result + "{:0>16}:\n".format(dp.id)
            result = result + "=" * 17 + "\n"

            for flow in flows:
                result = result + json.dumps(flow) + "\n"

        return result
