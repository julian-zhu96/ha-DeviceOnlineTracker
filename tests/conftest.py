"""Fixtures for Device Online Tracker tests."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

# pytest-homeassistant-custom-component 提供的 fixture
pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture
def mock_ping_online():
    """Mock async_ping returning online status."""
    mock_result = MagicMock()
    mock_result.packets_received = 3
    mock_result.packets_sent = 3
    
    with patch(
        "custom_components.DeviceOnlineTracker.async_ping",
        new_callable=AsyncMock,
        return_value=mock_result
    ) as mock:
        yield mock


@pytest.fixture
def mock_ping_offline():
    """Mock async_ping returning offline status."""
    mock_result = MagicMock()
    mock_result.packets_received = 0
    mock_result.packets_sent = 3
    
    with patch(
        "custom_components.DeviceOnlineTracker.async_ping",
        new_callable=AsyncMock,
        return_value=mock_result
    ) as mock:
        yield mock


@pytest.fixture
def mock_ping_sequence():
    """Mock async_ping with controllable sequence of results."""
    results = []
    call_count = [0]
    
    async def mock_ping(*args, **kwargs):
        mock_result = MagicMock()
        if call_count[0] < len(results):
            mock_result.packets_received = results[call_count[0]]
        else:
            mock_result.packets_received = 0
        mock_result.packets_sent = 3
        call_count[0] += 1
        return mock_result
    
    with patch(
        "custom_components.DeviceOnlineTracker.async_ping",
        side_effect=mock_ping
    ) as mock:
        mock.results = results
        mock.call_count_list = call_count
        yield mock


@pytest.fixture
def mock_store():
    """Mock Home Assistant storage."""
    with patch(
        "custom_components.DeviceOnlineTracker.Store"
    ) as mock:
        instance = mock.return_value
        instance.async_load = AsyncMock(return_value={})
        instance.async_save = AsyncMock()
        yield instance
