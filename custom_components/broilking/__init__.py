"""The Broil King Smoker integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BroilKingClient
from .const import DOMAIN
from .coordinator import BroilKingCoordinator
from .discover_schema import DISCOVER_SCHEMA

# On-demand RPC.List. The schema rejects every other field, including an RPC name.
SERVICE_DISCOVER = "discover_commands"

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Broil King Smoker from a config entry."""
    session = async_get_clientsession(hass)
    client = BroilKingClient(
        entry.data[CONF_HOST],
        session,
        entry.data.get(CONF_PASSWORD, ""),
    )
    device_id = entry.unique_id or entry.data[CONF_HOST]
    coordinator = BroilKingCoordinator(hass, client, device_id)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_ensure_discover_service(hass)
    return True


def _async_ensure_discover_service(hass: HomeAssistant) -> None:
    """Register the read-only command list. Extra service fields are rejected."""
    if hass.services.has_service(DOMAIN, SERVICE_DISCOVER):
        return

    async def _discover(call) -> dict:
        loaded = hass.data.get(DOMAIN) or {}
        entry_id = call.data.get("entry_id")
        if entry_id is not None:
            coordinator = loaded.get(entry_id)
            if coordinator is None:
                raise HomeAssistantError("Unknown Broil King config entry")
        elif len(loaded) == 1:
            coordinator = next(iter(loaded.values()))
        elif not loaded:
            raise HomeAssistantError("Broil King is not loaded")
        else:
            raise HomeAssistantError(
                "entry_id is required when more than one Broil King is loaded"
            )
        return await coordinator.client.async_discover_commands()

    hass.services.async_register(
        DOMAIN,
        SERVICE_DISCOVER,
        _discover,
        schema=DISCOVER_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_DISCOVER)
    return unloaded
