# Mikrotik Dashboard Configuration
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- Mikrotik native API connection ---
MIKROTIK_HOST = os.getenv('MIKROTIK_HOST', '192.168.88.1')
MIKROTIK_USER = os.getenv('MIKROTIK_USER', 'admin')
MIKROTIK_PASS = os.getenv('MIKROTIK_PASS', '')

# Use API-SSL (port 8729) or plain API (port 8728)
MIKROTIK_USE_SSL = os.getenv('MIKROTIK_USE_SSL', 'false').lower() == 'true'
MIKROTIK_API_PORT = int(os.getenv('MIKROTIK_API_PORT', '8729' if MIKROTIK_USE_SSL else '8728'))
MIKROTIK_API_TIMEOUT = int(os.getenv('MIKROTIK_API_TIMEOUT', '10'))

# --- WAN link detection ---
# Optional override. Comma-separated interface names, e.g. "ether1,pppoe-out1"
WAN_INTERFACES = [x.strip() for x in os.getenv('WAN_INTERFACES', '').split(',') if x.strip()]

# Friendly names for the WAN links (comma-separated, matching WAN_INTERFACES order)
WAN_LABELS = [x.strip() for x in os.getenv('WAN_LABELS', 'Link 1,Link 2').split(',') if x.strip()]

# Address used to test latency through each WAN interface.
# "gateway" = ping the next-hop of each link (works for failover/backup links)
# "public"  = ping WAN_PING_TARGET through each interface
WAN_PING_MODE = os.getenv('WAN_PING_MODE', 'gateway').strip().lower()
WAN_PING_TARGET = os.getenv('WAN_PING_TARGET', '8.8.8.8')

# --- Dashboard settings ---
REFRESH_INTERVAL = int(os.getenv('REFRESH_INTERVAL', '5'))
MAX_DATA_POINTS = int(os.getenv('MAX_DATA_POINTS', '60'))
LATENCY_WARNING_MS = float(os.getenv('LATENCY_WARNING_MS', '80'))
LATENCY_CRITICAL_MS = float(os.getenv('LATENCY_CRITICAL_MS', '150'))