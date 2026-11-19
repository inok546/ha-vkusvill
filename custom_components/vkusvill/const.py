"""Constants for the VkusVill integration."""

DOMAIN = "vkusvill"
NAME = "ВкусВилл"
VERSION = "0.1.0"

MCP_URL = "https://mcp.vkusvill.ru/mcp"
MCP_ORIGIN = "https://mcp.vkusvill.ru"
OAUTH_ORIGIN = "https://oauth.vkusvill.ru"
ORDER_TOOL = "vkusvill_orders_history"
REGISTRATION_URL = "https://vkusvill.ru/oauth/registration/"
MANUAL_REDIRECT_URI = "http://localhost:3000/callback"
ORDERS_SCOPE = "purchase_history.read"
REQUESTED_SCOPE = f"{ORDERS_SCOPE} offline_access"

CONF_TOKEN = "token"
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 120
MIN_SCAN_INTERVAL = 60
MAX_SCAN_INTERVAL = 3600
