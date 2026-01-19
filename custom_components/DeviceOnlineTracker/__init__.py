"""Device Online Tracker integration."""
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, cast, Tuple, Optional
import json
import subprocess
import re
import netifaces

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.DEBUG)
_LOGGER.info("Loading Device Online Tracker integration")

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_NAME,
    CONF_HOST,
    CONF_SCAN_INTERVAL,
    Platform
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.helpers.storage import Store
import voluptuous as vol

from icmplib import async_ping

DOMAIN = "device_online_tracker"
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_data"

# 配置常量
CONF_OFFLINE_THRESHOLD = "offline_threshold"
CONF_PING_COUNT = "ping_count"
CONF_RETRY_INTERVAL = "retry_interval"
CONF_RETRY_PING_COUNT = "retry_ping_count"
CONF_ENABLED = "enabled"

# 检测模式
MODE_PARALLEL = "parallel"  # 并行ping（默认，快速）
MODE_RETRY = "retry"  # 快速重试（更可靠）

# 默认值
DEFAULT_SCAN_INTERVAL = 60  # 秒
DEFAULT_OFFLINE_THRESHOLD = 3  # 连续失败次数（跨周期）
DEFAULT_PING_COUNT = 3  # 单次检测发送的ping包数量
DEFAULT_RETRY_INTERVAL = 5  # 快速重试间隔（秒）
DEFAULT_RETRY_PING_COUNT = 1  # 重试时发送的ping包数量（更快）
DEFAULT_ENABLED = True  # 默认启用检测

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

async def async_setup(hass: HomeAssistant, config: Dict[str, Any]) -> bool:
    """Set up the Device Online Tracker component."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[f"{DOMAIN}_config"] = {}  # 存储每个设备的配置
    
    async def async_ping_all_devices(call):
        """Handle the service call to ping all devices."""
        _LOGGER.debug("Service call received: %s", call.service)
        
        mode = call.data.get("mode", MODE_PARALLEL)
        _LOGGER.info("触发所有设备检测，模式: %s", mode)
        
        # 触发所有协调器的刷新
        for entry_id, coordinator in hass.data[DOMAIN].items():
            if isinstance(coordinator, DataUpdateCoordinator):
                # 获取设备配置
                device_config = hass.data[f"{DOMAIN}_config"].get(entry_id, {})
                
                # 检查设备是否启用
                if not device_config.get("enabled", DEFAULT_ENABLED):
                    _LOGGER.debug("设备 %s 已禁用，跳过检测", coordinator.name)
                    continue
                
                host = device_config.get("host")
                device_data = device_config.get("device_data")
                store = device_config.get("store")
                offline_threshold = device_config.get("offline_threshold", DEFAULT_OFFLINE_THRESHOLD)
                ping_count = device_config.get("ping_count", DEFAULT_PING_COUNT)
                retry_interval = device_config.get("retry_interval", DEFAULT_RETRY_INTERVAL)
                retry_ping_count = device_config.get("retry_ping_count", DEFAULT_RETRY_PING_COUNT)
                
                if host and device_data is not None and store:
                    await update_device_data(
                        device_data, host, store, entry_id,
                        offline_threshold=offline_threshold,
                        ping_count=ping_count,
                        retry_interval=retry_interval,
                        retry_ping_count=retry_ping_count,
                        mode=mode
                    )
                    coordinator.async_set_updated_data(device_data)
                else:
                    await coordinator.async_refresh()
        
        _LOGGER.info("已完成所有设备的在线状态检查")
    
    async def async_ping_single_device(call):
        """Handle the service call to ping a single device."""
        _LOGGER.debug("Service call received: %s with data %s", call.service, call.data)
        
        # 检查是否提供了设备名称或entry_id
        device_name = call.data.get("device_name")
        entry_id = call.data.get("entry_id")
        mode = call.data.get("mode", MODE_PARALLEL)
        
        if not device_name and not entry_id:
            _LOGGER.error("服务调用缺少参数：需要提供 device_name 或 entry_id")
            return
        
        _LOGGER.info("触发单设备检测，模式: %s", mode)
        
        # 查找目标协调器
        target_entries = []
        
        for current_entry_id, coordinator in hass.data[DOMAIN].items():
            if isinstance(coordinator, DataUpdateCoordinator):
                if entry_id and entry_id == current_entry_id:
                    target_entries.append((current_entry_id, coordinator))
                    break
                elif device_name:
                    # 从协调器的名称中提取设备名称
                    if device_name.lower() in coordinator.name.lower():
                        target_entries.append((current_entry_id, coordinator))
        
        if not target_entries:
            _LOGGER.warning("未找到匹配的设备：device_name='%s', entry_id='%s'", device_name, entry_id)
            return
        
        # 触发目标协调器的刷新
        for current_entry_id, coordinator in target_entries:
            # 获取设备配置
            device_config = hass.data[f"{DOMAIN}_config"].get(current_entry_id, {})
            
            # 检查设备是否启用
            if not device_config.get("enabled", DEFAULT_ENABLED):
                _LOGGER.warning("设备 %s 已禁用，跳过检测", coordinator.name)
                continue
            
            host = device_config.get("host")
            device_data = device_config.get("device_data")
            store = device_config.get("store")
            offline_threshold = device_config.get("offline_threshold", DEFAULT_OFFLINE_THRESHOLD)
            ping_count = device_config.get("ping_count", DEFAULT_PING_COUNT)
            retry_interval = device_config.get("retry_interval", DEFAULT_RETRY_INTERVAL)
            retry_ping_count = device_config.get("retry_ping_count", DEFAULT_RETRY_PING_COUNT)
            
            if host and device_data is not None and store:
                await update_device_data(
                    device_data, host, store, current_entry_id,
                    offline_threshold=offline_threshold,
                    ping_count=ping_count,
                    retry_interval=retry_interval,
                    retry_ping_count=retry_ping_count,
                    mode=mode
                )
                coordinator.async_set_updated_data(device_data)
            else:
                await coordinator.async_refresh()
            
            _LOGGER.info("已完成设备 %s 的在线状态检查", coordinator.name)
    
    # 注册服务
    hass.services.async_register(
        DOMAIN,
        "ping_all",
        async_ping_all_devices,
        schema=vol.Schema({
            vol.Optional("mode", default=MODE_PARALLEL): vol.In([MODE_PARALLEL, MODE_RETRY])
        })
    )
    
    hass.services.async_register(
        DOMAIN,
        "ping_device",
        async_ping_single_device,
        schema=vol.Schema({
            vol.Optional("device_name"): str,
            vol.Optional("entry_id"): str,
            vol.Optional("mode", default=MODE_PARALLEL): vol.In([MODE_PARALLEL, MODE_RETRY])
        })
    )
    
    return True

def get_default_interface() -> str:
    """获取默认网络接口
    
    Returns:
        str: 默认网络接口名称
    """
    try:
        # 获取默认网关接口
        gateways = netifaces.gateways()
        default_gateway = gateways['default'][netifaces.AF_INET][1]
        _LOGGER.debug("获取到默认网络接口: %s", default_gateway)
        return default_gateway
    except Exception as err:
        _LOGGER.error("获取默认网络接口失败: %s", err)
        # 如果获取失败，返回第一个非回环接口
        for interface in netifaces.interfaces():
            if interface != 'lo':
                _LOGGER.debug("使用备选网络接口: %s", interface)
                return interface
        return 'eth0'  # 最后的默认值

def get_ip_from_mac(mac_address: str) -> Optional[str]:
    """通过MAC地址获取IP地址
    
    Args:
        mac_address: MAC地址
        
    Returns:
        Optional[str]: IP地址，如果未找到则返回None
    """
    try:
        # 获取ARP表
        result = subprocess.run(
            ["ip", "neigh", "show"],
            capture_output=True,
            text=True,
            check=True
        )
        
        # 将MAC地址转换为小写并替换分隔符
        mac_pattern = mac_address.lower().replace(':', '')
        
        # 在ARP表中查找MAC地址
        for line in result.stdout.splitlines():
            if mac_pattern in line.lower().replace(':', ''):
                # 提取IP地址
                ip_match = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', line)
                if ip_match:
                    ip_address = ip_match.group(1)
                    _LOGGER.debug("找到MAC地址 %s 对应的IP地址: %s", mac_address, ip_address)
                    return ip_address
        
        _LOGGER.warning("未找到MAC地址 %s 对应的IP地址", mac_address)
        return None
    except Exception as err:
        _LOGGER.error("获取IP地址时出错: %s", err)
        return None

async def check_device_status(host: str, ping_count: int = DEFAULT_PING_COUNT) -> Tuple[bool, datetime]:
    """检查设备在线状态（并行发送多个ping包）
    
    Args:
        host: 设备主机地址或MAC地址（格式：xx:xx:xx:xx:xx:xx）
        ping_count: 发送的ping包数量，任意一个成功即判定在线
        
    Returns:
        Tuple[bool, datetime]: 返回设备是否在线和检查时间
    """
    try:
        current_time = datetime.now()
        
        # 检查是否为MAC地址
        if ":" in host:
            # 先获取MAC地址对应的IP
            ip_address = get_ip_from_mac(host)
            if ip_address:
                _LOGGER.debug("开始检测MAC地址: %s, 对应IP: %s, ping_count=%d", 
                             host, ip_address, ping_count)
                # 使用ping检测IP地址，发送多个包
                host_ping = await async_ping(ip_address, count=ping_count, timeout=2, interval=0.5)
                # 只要有一个包收到响应就算在线
                is_online = host_ping.packets_received > 0
                _LOGGER.debug("MAC地址 %s (IP: %s) 检测结果: %s (收到 %d/%d 包)", 
                            host, ip_address, "在线" if is_online else "离线",
                            host_ping.packets_received, ping_count)
            else:
                _LOGGER.warning("无法获取MAC地址 %s 对应的IP地址，尝试直接ping", host)
                # 如果无法获取IP，尝试直接ping MAC地址
                try:
                    interface = get_default_interface()
                    result = subprocess.run(
                        ["ping", "-c", str(ping_count), "-w", "3", host],
                        capture_output=True,
                        text=True,
                        check=False
                    )
                    is_online = result.returncode == 0
                except Exception as err:
                    _LOGGER.error("ping MAC地址失败: %s", err)
                    is_online = False
        else:
            # 使用ping检测IP地址
            _LOGGER.debug("开始检测IP地址: %s, ping_count=%d", host, ping_count)
            host_ping = await async_ping(host, count=ping_count, timeout=2, interval=0.5)
            # 只要有一个包收到响应就算在线
            is_online = host_ping.packets_received > 0
            _LOGGER.debug("IP地址 %s 检测结果: %s (收到 %d/%d 包)", 
                         host, "在线" if is_online else "离线",
                         host_ping.packets_received, ping_count)
            
        return is_online, current_time
    except Exception as err:
        _LOGGER.error("检查设备状态时出错: %s", err)
        return False, datetime.now()

async def update_device_data(
    device_data: Dict[str, Any], 
    host: str, 
    store: Store, 
    entry_id: str,
    offline_threshold: int = DEFAULT_OFFLINE_THRESHOLD,
    ping_count: int = DEFAULT_PING_COUNT,
    retry_interval: float = DEFAULT_RETRY_INTERVAL,
    retry_ping_count: int = DEFAULT_RETRY_PING_COUNT,
    mode: str = MODE_PARALLEL
) -> Dict[str, Any]:
    """更新设备数据（带防抖机制）
    
    支持两种检测模式：
    1. parallel（并行ping，默认）：单次发送多个ping包，快速返回
    2. retry（快速重试）：检测到离线时立即连续重试确认，更可靠
    
    Args:
        device_data: 当前设备数据
        host: 设备主机地址
        store: 存储对象
        entry_id: 配置条目ID
        offline_threshold: 连续失败多少次（跨周期）才判定离线
        ping_count: 单次检测发送的ping包数量
        retry_interval: 快速重试模式下的重试间隔（秒）
        retry_ping_count: 重试时发送的ping包数量
        mode: 检测模式，"parallel" 或 "retry"
        
    Returns:
        Dict[str, Any]: 更新后的设备数据
    """
    try:
        is_online, current_time = await check_device_status(host, ping_count)
        current_date = current_time.date()
        was_online = device_data.get("is_online", False)
        
        # 初始化失败计数器
        if "fail_count" not in device_data:
            device_data["fail_count"] = 0
        
        # 防抖逻辑
        if is_online:
            # 设备在线，重置失败计数
            device_data["fail_count"] = 0
            final_online_status = True
            _LOGGER.debug("设备 %s 检测在线，重置失败计数", host)
        else:
            # 设备检测离线
            device_data["fail_count"] += 1
            _LOGGER.debug("设备 %s 检测离线，失败计数: %d/%d, 模式: %s", 
                         host, device_data["fail_count"], offline_threshold, mode)
            
            # 快速重试模式：检测到离线时立即连续重试
            if mode == MODE_RETRY and was_online and device_data["fail_count"] < offline_threshold:
                _LOGGER.info("设备 %s 从在线变为离线，开始快速重试确认 (%d/%d)", 
                            host, device_data["fail_count"], offline_threshold)
                
                for retry in range(offline_threshold - device_data["fail_count"]):
                    await asyncio.sleep(retry_interval)
                    retry_online, _ = await check_device_status(host, retry_ping_count)
                    
                    if retry_online:
                        device_data["fail_count"] = 0
                        _LOGGER.info("设备 %s 快速重试第 %d 次成功，确认在线", host, retry + 1)
                        break
                    else:
                        device_data["fail_count"] += 1
                        _LOGGER.debug("设备 %s 快速重试第 %d 次失败，失败计数: %d/%d", 
                                     host, retry + 1, device_data["fail_count"], offline_threshold)
            
            # 判断最终状态
            if device_data["fail_count"] >= offline_threshold:
                final_online_status = False
                _LOGGER.info("设备 %s 连续 %d 次检测失败，确认离线", 
                            host, device_data["fail_count"])
            else:
                # 还没达到阈值，保持之前的在线状态
                final_online_status = was_online
                _LOGGER.debug("设备 %s 失败次数未达阈值，保持状态: %s", 
                             host, "在线" if final_online_status else "离线")
        
        # 如果是新的一天，重置计时
        if current_date != device_data["last_date"]:
            device_data["online_time"] = 0
            device_data["last_date"] = current_date
        
        # 计算在线时间
        if device_data.get("last_check") and device_data.get("is_online") and final_online_status:
            time_diff = (current_time - device_data["last_check"]).total_seconds() / 60
            device_data["online_time"] += time_diff
        
        device_data["is_online"] = final_online_status
        device_data["last_check"] = current_time
        device_data["online_time"] = round(device_data.get("online_time", 0))
        
        # 保存数据到持久存储
        try:
            stored_data = await store.async_load() or {}
            save_data = {
                "online_time": device_data["online_time"],
                "last_date": device_data["last_date"].isoformat(),
                "is_online": device_data["is_online"],
                "fail_count": device_data["fail_count"]
            }
            
            if device_data["last_check"]:
                save_data["last_check"] = device_data["last_check"].isoformat()
            
            stored_data[entry_id] = save_data
            await store.async_save(stored_data)
        except Exception as save_err:
            _LOGGER.error("保存数据时出错: %s", save_err)
        
        return device_data
    except Exception as err:
        _LOGGER.error("更新设备数据时出错: %s", err)
        return device_data

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Device Online Tracker from a config entry."""
    device_name = entry.data[CONF_NAME]
    host = entry.data[CONF_HOST]
    
    # 从配置或选项中获取参数（优先使用选项）
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL, 
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    offline_threshold = entry.options.get(
        CONF_OFFLINE_THRESHOLD,
        entry.data.get(CONF_OFFLINE_THRESHOLD, DEFAULT_OFFLINE_THRESHOLD)
    )
    ping_count = entry.options.get(
        CONF_PING_COUNT,
        entry.data.get(CONF_PING_COUNT, DEFAULT_PING_COUNT)
    )
    retry_interval = entry.options.get(
        CONF_RETRY_INTERVAL,
        entry.data.get(CONF_RETRY_INTERVAL, DEFAULT_RETRY_INTERVAL)
    )
    retry_ping_count = entry.options.get(
        CONF_RETRY_PING_COUNT,
        entry.data.get(CONF_RETRY_PING_COUNT, DEFAULT_RETRY_PING_COUNT)
    )
    enabled = entry.options.get(
        CONF_ENABLED,
        entry.data.get(CONF_ENABLED, DEFAULT_ENABLED)
    )
    
    _LOGGER.info(
        "设备 %s 配置: enabled=%s, scan_interval=%ds, offline_threshold=%d, ping_count=%d, retry_interval=%ds, retry_ping_count=%d",
        device_name, enabled, scan_interval, offline_threshold, ping_count, retry_interval, retry_ping_count
    )
    
    # 创建存储对象
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    
    # 初始化设备数据
    device_data = {
        "online_time": 0,
        "last_check": None,
        "last_date": datetime.now().date().isoformat(),
        "is_online": False,
        "fail_count": 0
    }
    
    # 尝试从存储加载数据
    try:
        stored_data = await store.async_load()
        if stored_data and entry.entry_id in stored_data:
            entry_data = stored_data[entry.entry_id]
            
            # 检查是否是同一天的数据
            stored_date = datetime.strptime(
                entry_data["last_date"],
                "%Y-%m-%d"
            ).date()
            
            if stored_date == datetime.now().date():
                device_data["online_time"] = entry_data["online_time"]
                device_data["last_date"] = stored_date.isoformat()
                device_data["fail_count"] = entry_data.get("fail_count", 0)
                
                if entry_data.get("last_check"):
                    device_data["last_check"] = datetime.fromisoformat(entry_data["last_check"])
                
                _LOGGER.info("已从存储恢复设备数据: %s", device_data)
    except Exception as err:
        _LOGGER.error("加载存储数据时出错: %s", err)
    
    # 确保last_date是datetime.date对象
    if isinstance(device_data["last_date"], str):
        device_data["last_date"] = datetime.strptime(
            device_data["last_date"],
            "%Y-%m-%d"
        ).date()
    
    # 保存设备配置供API调用使用
    hass.data[f"{DOMAIN}_config"][entry.entry_id] = {
        "host": host,
        "device_data": device_data,
        "store": store,
        "offline_threshold": offline_threshold,
        "ping_count": ping_count,
        "retry_interval": retry_interval,
        "retry_ping_count": retry_ping_count,
        "enabled": enabled,
    }
    
    unique_id = f"{entry.entry_id}_status"
    entity_id = f"binary_sensor.{device_name.lower().replace(' ', '_')}_status"
    
    async def async_update_data():
        """Fetch data from API endpoint."""
        # 检查设备是否启用
        if not enabled:
            _LOGGER.debug("设备 %s 已禁用，跳过定时检测", device_name)
            return device_data
        
        return await update_device_data(
            device_data, host, store, entry.entry_id,
            offline_threshold=offline_threshold,
            ping_count=ping_count,
            retry_interval=retry_interval,
            retry_ping_count=retry_ping_count,
            mode=MODE_PARALLEL  # 定时扫描默认使用并行模式
        )

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"device_{device_name}",
        update_method=async_update_data,
        update_interval=timedelta(seconds=scan_interval),
    )

    # 立即获取第一次数据
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        hass.data[f"{DOMAIN}_config"].pop(entry.entry_id, None)

    return unload_ok

class DeviceOnlineTrackerEntity(CoordinatorEntity):
    """Representation of a Device Online Tracker entity."""

    def __init__(self, coordinator, config_entry, entity_type):
        """Initialize the entity."""
        super().__init__(coordinator)
        self.config_entry = config_entry
        self.entity_type = entity_type
        self._attr_has_entity_name = True
        self._attr_unique_id = f"{config_entry.entry_id}_{entity_type}"
        
    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.config_entry.entry_id)},
            name=self.config_entry.data[CONF_NAME],
            manufacturer="捣鼓程序员",
            model="Device Online Tracker",
            sw_version="1.0",
        )