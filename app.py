import os
import time
import re
import ssl
import threading
from flask import Flask, render_template, jsonify
from librouteros import connect
from config import *

app = Flask(__name__)

# --- Shared state -----------------------------------------------------------
metrics_lock = threading.Lock()
state = {
    'system': {},
    'interfaces': {},
    'vlans': [],
    'wan_links': {},
    'history': {
        'timestamps': [],
        'latency': {},
        'traffic': {},
        'cpu': [],
        'memory': [],
    },
    'last_update': None,
    'error': None,
}

# Keeps the last byte counters so we can compute rates.
last_interface_bytes = {}  # name -> (rx, tx, timestamp)
_last_wan_ifaces_debug = None

# --- Mikrotik native API ----------------------------------------------------
_api = None
_api_lock = threading.Lock()


def get_api():
    """Return a logged-in native API connection, creating it if needed."""
    global _api
    with _api_lock:
        if _api is None:
            kwargs = dict(
                host=MIKROTIK_HOST,
                username=MIKROTIK_USER,
                password=MIKROTIK_PASS,
                port=MIKROTIK_API_PORT,
                timeout=MIKROTIK_API_TIMEOUT,
            )
            if MIKROTIK_USE_SSL:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                kwargs['ssl_wrapper'] = ctx.wrap_socket
            _api = connect(**kwargs)
        return _api


def reset_api():
    global _api
    with _api_lock:
        if _api is not None:
            try:
                _api.close()
            except Exception:
                pass
        _api = None


def ros_bool(value):
    """RouterOS API values may arrive as bool, int or string."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ('true', 'yes', '1', 'on')
    return False


def parse_rtt_ms(rtt_str):
    """Parse RouterOS RTT strings like '453us', '12.5ms', '1s' to ms."""
    if not rtt_str:
        return None
    m = re.match(r'([\d.]+)\s*(us|ms|s)?', str(rtt_str).strip())
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2) or 'us'
    if unit == 'us':
        return value / 1000.0
    if unit == 'ms':
        return value
    return value * 1000.0


def get_ethernet_rate(api, iface_name):
    """Ask /interface/ethernet/monitor for the negotiated link rate."""
    try:
        rows = list(api('/interface/ethernet/monitor', **{'.id': iface_name, 'once': ''}))
        if rows:
            return rows[0].get('rate', '')
    except Exception:
        pass
    return ''


def ping_through_interface(api, target, interface=None, vrf=None):
    """Ping a target forcing the packet out of an interface or VRF."""
    base = {'address': target, 'count': '4'}
    if vrf:
        base['vrf'] = vrf
    elif interface:
        base['interface'] = interface

    try:
        rows = list(api('/ping', interval='200ms', **base))
    except Exception as e:
        where = f'vrf={vrf}' if vrf else f'interface={interface}'
        print(f'Ping error on {where} (with interval): {e}')
        try:
            rows = list(api('/ping', **base))
        except Exception as e2:
            print(f'Ping error on {where} (fallback): {e2}')
            return None, None

    rtts = []
    lost = 0
    for row in rows:
        # Some RouterOS versions return a summary row with avg-rtt/packet-loss.
        if 'avg-rtt' in row:
            avg = parse_rtt_ms(row.get('avg-rtt'))
            loss_raw = row.get('packet-loss', '0')
            try:
                loss = float(str(loss_raw).replace('%', ''))
            except ValueError:
                loss = None
            return avg, loss
        time_str = row.get('time', '')
        if time_str:
            ms = parse_rtt_ms(time_str)
            if ms is not None:
                rtts.append(ms)
        elif row.get('status') == 'timeout':
            lost += 1

    avg = sum(rtts) / len(rtts) if rtts else None
    loss = (lost / len(rows) * 100) if rows else None
    return avg, loss


def route_endpoint(route):
    """Extract (interface, next_hop_ip) from a RouterOS route row.

    RouterOS recursive routes put the real next-hop in ``immediate-gw``
    (for example ``172.17.1.1%ether3-BRD``), while ``gateway`` may contain
    a recursive/public address.  Prefer ``immediate-gw`` so latency tests
    always ping the directly connected next-hop.
    """
    iface = route.get('interface', '') or ''
    immediate = str(route.get('immediate-gw', '') or '')
    gateway = str(route.get('gateway', '') or '')

    if immediate:
        if '%' in immediate:
            gw_ip, iface_suffix = immediate.split('%', 1)
            if not iface:
                iface = iface_suffix
            return iface, gw_ip
        return iface, immediate

    if '%' in gateway:
        gateway_ip, iface_suffix = gateway.split('%', 1)
        if not iface:
            iface = iface_suffix
        return iface, gateway_ip

    return iface, gateway


def find_vrf_for_interface(routes, interfaces, iface_name):
    """Find a VRF/routing-table associated with a WAN interface.

    Used to measure public-internet latency on failover/backup links, where
    the main table route points to the primary link.
    """
    candidates = []
    for r in routes:
        if r.get('dst-address') != '0.0.0.0/0':
            continue
        route_iface, _ = route_endpoint(r)
        if route_iface != iface_name:
            continue
        table = r.get('routing-table', 'main')
        if table and table != 'main' and table not in candidates:
            candidates.append(table)

    # Prefer a real VRF interface (type=vrf) over a plain routing table.
    for table in candidates:
        if table in interfaces and interfaces[table].get('type') == 'vrf':
            return table
    return candidates[0] if candidates else ''


def get_wan_interfaces(routes, addresses):
    """Detect WAN interfaces from active default routes or config override."""
    if WAN_INTERFACES:
        return WAN_INTERFACES

    # Include all usable default routes, not only the active one.
    # This is important for failover setups, where the backup route is
    # present but inactive while the primary link is healthy.
    default_routes = [
        r for r in routes
        if r.get('dst-address') == '0.0.0.0/0'
        and not ros_bool(r.get('disabled'))
        and not ros_bool(r.get('invalid'))
    ]

    def route_priority(r):
        # Active routes first, then lower distance.
        active = 0 if ros_bool(r.get('active')) else 1
        try:
            distance = int(r.get('distance', 1))
        except (TypeError, ValueError):
            distance = 1
        return (active, distance)

    default_routes.sort(key=route_priority)

    wan = []
    for r in default_routes:
        iface, gateway = route_endpoint(r)

        if not iface and gateway:
            for addr in addresses:
                if str(addr.get('address', '')).split('/')[0] == gateway:
                    iface = addr.get('interface', '')
                    break

        if iface and iface not in wan:
            wan.append(iface)
    return wan


# --- Data collection --------------------------------------------------------
def collect_once():
    """Collect a full snapshot from the router and update shared state."""
    api = get_api()
    now = time.time()

    # System resources -------------------------------------------------------
    resource = list(api('/system/resource/print'))
    resource = resource[0] if resource else {}
    total_mem = int(resource.get('total-memory', 0) or 0)
    free_mem = int(resource.get('free-memory', 0) or 0)
    used_mem = max(total_mem - free_mem, 0)
    system = {
        'cpu_load': int(resource.get('cpu-load', 0) or 0),
        'uptime': resource.get('uptime', ''),
        'version': resource.get('version', ''),
        'board_name': resource.get('board-name', ''),
        'architecture': resource.get('architecture-name', ''),
        'cpu_count': resource.get('cpu-count', ''),
        'cpu_frequency': resource.get('cpu-frequency', ''),
        'total_memory': total_mem,
        'free_memory': free_mem,
        'used_memory': used_mem,
        'memory_percent': (used_mem / total_mem * 100.0) if total_mem else 0.0,
    }

    try:
        health_rows = list(api('/system/health/print'))
        health = health_rows[0] if health_rows else {}
    except Exception:
        health = {}
    system['temperature'] = (
        health.get('temperature')
        or health.get('cpu-temperature')
        or health.get('board-temperature')
        or ''
    )

    # Extra counters ---------------------------------------------------------
    try:
        rows = list(api('/ip/firewall/connection/print', **{'count-only': ''}))
        system['active_connections'] = int(rows[-1].get('ret', 0)) if rows else 0
    except Exception:
        system['active_connections'] = None

    try:
        rows = list(api('/ip/dhcp-server/lease/print', **{'count-only': ''}))
        system['dhcp_leases'] = int(rows[-1].get('ret', 0)) if rows else 0
    except Exception:
        system['dhcp_leases'] = None

    # Interfaces -------------------------------------------------------------
    iface_rows = list(api('/interface/print'))
    interfaces = {}
    for row in iface_rows:
        name = row.get('name', '')
        rx = int(row.get('rx-byte', 0) or 0)
        tx = int(row.get('tx-byte', 0) or 0)
        prev = last_interface_bytes.get(name)
        if prev:
            dt = now - prev[2]
            rx_rate = ((rx - prev[0]) / dt) if dt > 0 else 0
            tx_rate = ((tx - prev[1]) / dt) if dt > 0 else 0
        else:
            rx_rate = 0
            tx_rate = 0
        last_interface_bytes[name] = (rx, tx, now)
        interfaces[name] = {
            'name': name,
            'type': row.get('type', ''),
            'running': ros_bool(row.get('running')),
            'rx_byte': rx,
            'tx_byte': tx,
            'rx_rate': rx_rate,
            'tx_rate': tx_rate,
            'mtu': row.get('mtu', ''),
            'mac_address': row.get('mac-address', ''),
        }

    # VLANs ------------------------------------------------------------------
    vlan_rows = list(api('/interface/vlan/print'))
    vlans = []
    for row in vlan_rows:
        vlans.append({
            'name': row.get('name', ''),
            'vlan_id': row.get('vlan-id', ''),
            'interface': row.get('interface', ''),
            'running': ros_bool(row.get('running')),
            'mtu': row.get('mtu', ''),
            'l2mtu': row.get('l2mtu', ''),
        })
    vlans.sort(key=lambda v: int(v['vlan_id']) if str(v['vlan_id']).isdigit() else 0)

    # Routes / addresses -----------------------------------------------------
    routes = list(api('/ip/route/print'))
    addresses = list(api('/ip/address/print'))

    wan_ifaces = get_wan_interfaces(routes, addresses)

    global _last_wan_ifaces_debug
    if wan_ifaces != _last_wan_ifaces_debug:
        print(f'WAN interfaces detected: {wan_ifaces}')
        _last_wan_ifaces_debug = list(wan_ifaces)

    # WAN links --------------------------------------------------------------
    wan_links = {}
    for idx, iface_name in enumerate(wan_ifaces):
        iface_info = interfaces.get(iface_name, {})

        ip = ''
        for addr in addresses:
            if addr.get('interface') == iface_name:
                ip = addr.get('address', '')
                break

        gateway = ''
        for r in routes:
            if r.get('dst-address') != '0.0.0.0/0':
                continue
            route_iface, route_gw = route_endpoint(r)
            if route_iface == iface_name:
                gateway = route_gw
                break

        link_rate = ''
        if iface_info.get('type') == 'ether':
            link_rate = get_ethernet_rate(api, iface_name)

        # For failover/backup links there may be no active default route,
        # so pinging a public address can fail. The gateway is directly
        # connected and is a reliable health/latency probe.
        vrf = ''
        if WAN_PING_MODE == 'public':
            ping_target = WAN_PING_TARGET
            vrf = find_vrf_for_interface(routes, interfaces, iface_name)
            latency, packet_loss = ping_through_interface(
                api, ping_target, vrf=vrf, interface=iface_name if not vrf else None
            )
        else:
            ping_target = gateway or WAN_PING_TARGET
            latency, packet_loss = ping_through_interface(
                api, ping_target, interface=iface_name
            )

        label = WAN_LABELS[idx] if idx < len(WAN_LABELS) else iface_name
        wan_links[iface_name] = {
            'label': label,
            'interface': iface_name,
            'running': iface_info.get('running', False),
            'ip': ip,
            'gateway': gateway,
            'ping_target': ping_target,
            'vrf': vrf,
            'link_rate': link_rate,
            'latency': latency,
            'packet_loss': packet_loss,
            'rx_rate': iface_info.get('rx_rate', 0),
            'tx_rate': iface_info.get('tx_rate', 0),
            'type': iface_info.get('type', ''),
        }

    # History ----------------------------------------------------------------
    with metrics_lock:
        state['system'] = system
        state['interfaces'] = interfaces
        state['vlans'] = vlans
        state['wan_links'] = wan_links
        state['last_update'] = now
        state['error'] = None

        hist = state['history']
        hist['timestamps'].append(now)
        if len(hist['timestamps']) > MAX_DATA_POINTS:
            hist['timestamps'] = hist['timestamps'][-MAX_DATA_POINTS:]

        for iface_name, info in wan_links.items():
            hist['latency'].setdefault(iface_name, [])
            hist['latency'][iface_name].append(info['latency'])
            if len(hist['latency'][iface_name]) > MAX_DATA_POINTS:
                hist['latency'][iface_name] = hist['latency'][iface_name][-MAX_DATA_POINTS:]

        for iface_name in wan_ifaces:
            iface_info = interfaces.get(iface_name, {})
            hist['traffic'].setdefault(iface_name, {'rx': [], 'tx': []})
            hist['traffic'][iface_name]['rx'].append(iface_info.get('rx_rate', 0))
            hist['traffic'][iface_name]['tx'].append(iface_info.get('tx_rate', 0))
            if len(hist['traffic'][iface_name]['rx']) > MAX_DATA_POINTS:
                hist['traffic'][iface_name]['rx'] = hist['traffic'][iface_name]['rx'][-MAX_DATA_POINTS:]
            if len(hist['traffic'][iface_name]['tx']) > MAX_DATA_POINTS:
                hist['traffic'][iface_name]['tx'] = hist['traffic'][iface_name]['tx'][-MAX_DATA_POINTS:]

        hist['cpu'].append(system['cpu_load'])
        hist['memory'].append(system['memory_percent'])
        if len(hist['cpu']) > MAX_DATA_POINTS:
            hist['cpu'] = hist['cpu'][-MAX_DATA_POINTS:]
        if len(hist['memory']) > MAX_DATA_POINTS:
            hist['memory'] = hist['memory'][-MAX_DATA_POINTS:]


def collector_loop():
    while True:
        try:
            collect_once()
        except Exception as e:
            print(f'Collector error: {e}')
            with metrics_lock:
                state['error'] = str(e)
            reset_api()
        finally:
            time.sleep(REFRESH_INTERVAL)


# Start collector thread (can be disabled for tests/tools)
if os.getenv('MIKROKING_NO_COLLECTOR', 'false').lower() != 'true':
    collector_thread = threading.Thread(target=collector_loop, daemon=True)
    collector_thread.start()


# --- Flask routes -----------------------------------------------------------
@app.route('/')
def index():
    return render_template(
        'base.html',
        REFRESH_INTERVAL=REFRESH_INTERVAL,
        LATENCY_WARNING_MS=LATENCY_WARNING_MS,
        LATENCY_CRITICAL_MS=LATENCY_CRITICAL_MS,
    )


@app.route('/api/metrics')
def api_metrics():
    with metrics_lock:
        # Build JSON-safe copy
        wan_links = {}
        for name, info in state['wan_links'].items():
            wan_links[name] = dict(info)

        history = {
            'timestamps': list(state['history']['timestamps']),
            'latency': {k: list(v) for k, v in state['history']['latency'].items()},
            'traffic': {
                k: {'rx': list(v['rx']), 'tx': list(v['tx'])}
                for k, v in state['history']['traffic'].items()
            },
            'cpu': list(state['history']['cpu']),
            'memory': list(state['history']['memory']),
        }

        return jsonify({
            'system': dict(state['system']),
            'interfaces': {k: dict(v) for k, v in state['interfaces'].items()},
            'vlans': list(state['vlans']),
            'wan_links': wan_links,
            'history': history,
            'last_update': state['last_update'],
            'error': state['error'],
        })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)