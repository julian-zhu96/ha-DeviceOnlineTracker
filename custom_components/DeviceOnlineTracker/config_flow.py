"""Config flow for Device Online Tracker integration."""
import logging
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME, CONF_HOST, CONF_SCAN_INTERVAL
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

from . import (
    DOMAIN,
    CONF_OFFLINE_THRESHOLD,
    CONF_PING_COUNT,
    CONF_RETRY_PING_COUNT,
    CONF_ENABLED,
    CONF_DETECTION_METHOD,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_OFFLINE_THRESHOLD,
    DEFAULT_PING_COUNT,
    DEFAULT_RETRY_PING_COUNT,
    DEFAULT_ENABLED,
    DEFAULT_DETECTION_METHOD,
    DETECTION_PING,
    DETECTION_ARP,
    DETECTION_AUTO,
)

_LOGGER = logging.getLogger(__name__)

class DeviceOnlineTrackerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Device Online Tracker."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            # 验证输入
            try:
                # 确保设备名称唯一
                await self.async_set_unique_id(user_input[CONF_NAME])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data=user_input
                )
            except Exception as err:
                _LOGGER.error("Error creating device: %s", err)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME): str,
                vol.Required(CONF_HOST): str,
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=3600)
                ),
                vol.Optional(CONF_OFFLINE_THRESHOLD, default=DEFAULT_OFFLINE_THRESHOLD): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=10)
                ),
                vol.Optional(CONF_PING_COUNT, default=DEFAULT_PING_COUNT): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=10)
                ),
                vol.Optional(CONF_RETRY_PING_COUNT, default=DEFAULT_RETRY_PING_COUNT): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=10)
                ),
                vol.Optional(CONF_DETECTION_METHOD, default=DEFAULT_DETECTION_METHOD): vol.In(
                    [DETECTION_PING, DETECTION_ARP, DETECTION_AUTO]
                ),
            }),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)

class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Device Online Tracker."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # 获取当前值
        current_scan_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        current_offline_threshold = self.config_entry.options.get(
            CONF_OFFLINE_THRESHOLD,
            self.config_entry.data.get(CONF_OFFLINE_THRESHOLD, DEFAULT_OFFLINE_THRESHOLD)
        )
        current_ping_count = self.config_entry.options.get(
            CONF_PING_COUNT,
            self.config_entry.data.get(CONF_PING_COUNT, DEFAULT_PING_COUNT)
        )
        current_retry_ping_count = self.config_entry.options.get(
            CONF_RETRY_PING_COUNT,
            self.config_entry.data.get(CONF_RETRY_PING_COUNT, DEFAULT_RETRY_PING_COUNT)
        )
        current_enabled = self.config_entry.options.get(
            CONF_ENABLED,
            self.config_entry.data.get(CONF_ENABLED, DEFAULT_ENABLED)
        )
        current_detection_method = self.config_entry.options.get(
            CONF_DETECTION_METHOD,
            self.config_entry.data.get(CONF_DETECTION_METHOD, DEFAULT_DETECTION_METHOD)
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_ENABLED, default=current_enabled): bool,
                vol.Optional(CONF_SCAN_INTERVAL, default=current_scan_interval): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=3600)
                ),
                vol.Optional(CONF_OFFLINE_THRESHOLD, default=current_offline_threshold): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=10)
                ),
                vol.Optional(CONF_PING_COUNT, default=current_ping_count): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=10)
                ),
                vol.Optional(CONF_RETRY_PING_COUNT, default=current_retry_ping_count): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=10)
                ),
                vol.Optional(CONF_DETECTION_METHOD, default=current_detection_method): vol.In(
                    [DETECTION_PING, DETECTION_ARP, DETECTION_AUTO]
                ),
            })
        ) 