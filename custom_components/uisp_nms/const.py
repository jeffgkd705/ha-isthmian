"""Constants for the UISP NMS integration."""
from typing import Final

DOMAIN: Final = "uisp_nms"

CONF_FINGERPRINT: Final = "fingerprint"

DEFAULT_SCAN_INTERVAL: Final = 30
DEFAULT_TIMEOUT: Final = 30
# Seconds to wait after a write before re-reading: UISP answers a PUT with the
# interface as it was before the change, and the device applies it a few
# seconds later.
REFRESH_AFTER_WRITE: Final = 5
# UISP is slow under load; keep the number of parallel requests small.
MAX_PARALLEL_REQUESTS: Final = 2

API_BASE: Final = "/nms/api/v2.1"
API_DEVICES: Final = f"{API_BASE}/devices"
HEADER_AUTH_TOKEN: Final = "x-auth-token"

DEVICE_STATUS_ACTIVE: Final = "active"
LINK_STATUS_ACTIVE: Final = "active"
POE_OFF: Final = "off"

# Interface types that are physical ports (switch ports and router ethernet).
PORT_INTERFACE_TYPES: Final = ("port", "eth", "sfp", "sfp+")

SERVICE_POWER_CYCLE_PORT: Final = "power_cycle_port"

# identification.type of EdgeRouters, whose ports are enabled with block/unblock.
DEVICE_TYPE_ROUTER = "erouter"

# URL of the bundled dashboard card.
CARD_URL = "/uisp_nms/uisp-nms-card.js"

# VLAN membership is read from the device detail, which is large and rarely
# changes, so less often than the regular poll (seconds).
VLAN_REFRESH_INTERVAL = 600
DEVICE_TYPE_SWITCH = "eswitch"
