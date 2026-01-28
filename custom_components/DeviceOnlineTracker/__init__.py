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
import homeassistant.helpers.config_validation as cv

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
CONF_MAX_CONCURRENT = "max_concurrent"  # 最大并发数
CONF_DETECTION_METHOD = "detection_method"  # 检测方式

# 检测模式（用于离线确认的重试策略）
MODE_PARALLEL = "parallel"  # 并行ping（默认，快速）
MODE_RETRY = "retry"  # 快速重试（更可靠）

# 检测方式（用于检测设备在线状态的方法）
DETECTION_PING = "ping"  # 仅使用 ICMP ping
DETECTION_ARP = "arp"    # 仅使用 ARP 检测（适合移动设备）
DETECTION_AUTO = "auto"  # 自动：先 ping，失败则 arp

# 默认值
DEFAULT_SCAN_INTERVAL = 60  # 秒
DEFAULT_OFFLINE_THRESHOLD = 3  # 连续失败次数（跨周期）
DEFAULT_PING_COUNT = 3  # 单次检测发送的ping包数量
DEFAULT_RETRY_INTERVAL = 5  # 快速重试间隔（秒）
DEFAULT_RETRY_PING_COUNT = 3  # 重试时发送的ping包数量
DEFAULT_ENABLED = True  # 默认启用检测
DEFAULT_MAX_CONCURRENT = 5  # 默认最大并发数
DEFAULT_DETECTION_METHOD = DETECTION_ARP  # 默认使用 ARP（适合移动设备）

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

# configuration.yaml 配置 schema
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema({
            vol.Optional(CONF_MAX_CONCURRENT, default=DEFAULT_MAX_CONCURRENT): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=50)
            ),
        })
    },
    extra=vol.ALLOW_EXTRA,
)

async def async_setup(hass: HomeAssistant, config: Dict[str, Any]) -> bool:
    """Set up the Device Online Tracker component."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[f"{DOMAIN}_config"] = {}  # 存储每个设备的配置
    
    # 从 configuration.yaml 读取全局配置
    global_config = config.get(DOMAIN, {})
    max_concurrent = global_config.get(CONF_MAX_CONCURRENT, DEFAULT_MAX_CONCURRENT)
    hass.data[f"{DOMAIN}_global"] = {
        CONF_MAX_CONCURRENT: max_concurrent,
    }
    _LOGGER.info("全局配置: max_concurrent=%d", max_concurrent)
    
    async def async_ping_all_devices(call):
        """Handle the service call to ping all devices (concurrent)."""
        _LOGGER.debug("Service call received: %s", call.service)
        
        mode = call.data.get("mode", MODE_PARALLEL)
        # 允许通过服务调用覆盖并发数
        concurrent_limit = call.data.get("max_concurrent", hass.data[f"{DOMAIN}_global"].get(CONF_MAX_CONCURRENT, DEFAULT_MAX_CONCURRENT))
        _LOGGER.info("触发所有设备检测，模式: %s，最大并发: %d", mode, concurrent_limit)
        
        # 收集所有需要检测的设备任务
        async def ping_device_task(entry_id: str, coordinator: DataUpdateCoordinator) -> None:
            """单个设备的检测任务"""
            device_config = hass.data[f"{DOMAIN}_config"].get(entry_id, {})
            
            # 检查设备是否启用
            if not device_config.get("enabled", DEFAULT_ENABLED):
                _LOGGER.debug("设备 %s 已禁用，跳过检测", coordinator.name)
                return
            
            host = device_config.get("host")
            device_data = device_config.get("device_data")
            store = device_config.get("store")
            offline_threshold = device_config.get("offline_threshold", DEFAULT_OFFLINE_THRESHOLD)
            ping_count = device_config.get("ping_count", DEFAULT_PING_COUNT)
            retry_interval = device_config.get("retry_interval", DEFAULT_RETRY_INTERVAL)
            retry_ping_count = device_config.get("retry_ping_count", DEFAULT_RETRY_PING_COUNT)
            detection_method = device_config.get("detection_method", DEFAULT_DETECTION_METHOD)
            
            if host and device_data is not None and store:
                await update_device_data(
                    device_data, host, store, entry_id,
                    offline_threshold=offline_threshold,
                    ping_count=ping_count,
                    retry_interval=retry_interval,
                    retry_ping_count=retry_ping_count,
                    mode=mode,
                    reset_fail_count=True,  # API调用时重置失败计数，立即判断状态
                    detection_method=detection_method
                )
                coordinator.async_set_updated_data(device_data)
            else:
                await coordinator.async_refresh()
        
        # 收集所有设备
        devices_to_ping = []
        for entry_id, coordinator in hass.data[DOMAIN].items():
            if isinstance(coordinator, DataUpdateCoordinator):
                devices_to_ping.append((entry_id, coordinator))
        
        # 使用信号量限制并发数
        semaphore = asyncio.Semaphore(concurrent_limit)
        
        async def limited_ping_task(entry_id: str, coordinator: DataUpdateCoordinator) -> None:
            """带并发限制的检测任务"""
            async with semaphore:
                await ping_device_task(entry_id, coordinator)
        
        # 并发执行所有设备检测
        if devices_to_ping:
            tasks = [limited_ping_task(entry_id, coord) for entry_id, coord in devices_to_ping]
            await asyncio.gather(*tasks, return_exceptions=True)
        
        _LOGGER.info("已完成所有设备的在线状态检查（并发模式，%d 个设备）", len(devices_to_ping))
    
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
            detection_method = device_config.get("detection_method", DEFAULT_DETECTION_METHOD)
            
            if host and device_data is not None and store:
                await update_device_data(
                    device_data, host, store, current_entry_id,
                    offline_threshold=offline_threshold,
                    ping_count=ping_count,
                    retry_interval=retry_interval,
                    retry_ping_count=retry_ping_count,
                    mode=mode,
                    reset_fail_count=True,  # API调用时重置失败计数，立即判断状态
                    detection_method=detection_method
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
            vol.Optional("mode", default=MODE_RETRY): vol.In([MODE_PARALLEL, MODE_RETRY])
        })
    )
    
    hass.services.async_register(
        DOMAIN,
        "ping_device",
        async_ping_single_device,
        schema=vol.Schema({
            vol.Optional("device_name"): str,
            vol.Optional("entry_id"): str,
            vol.Optional("mode", default=MODE_RETRY): vol.In([MODE_PARALLEL, MODE_RETRY])
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

async def check_arp_table(ip_address: str, device_name: str = None) -> bool:
    """检查 ARP 表中是否存在指定 IP 地址的有效条目
    
    Args:
        ip_address: 要检查的 IP 地址
        device_name: 设备名称，用于日志
        
    Returns:
        bool: ARP 表中是否存在有效条目（设备在线）
    """
    import platform
    system = platform.system().lower()
    
    try:
        if system == "linux":
            # Linux: 使用 ip neigh 或读取 /proc/net/arp
            # 尝试多种 ARP 表查看命令
            arp_commands = [
                ["ip", "neigh", "show", ip_address],  # 查看指定 IP
                ["ip", "neigh", "show"]  # 查看所有 ARP 条目
            ]
            
            for cmd in arp_commands:
                try:
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: subprocess.run(
                            cmd,
                            capture_output=True,
                            text=True,
                            timeout=5
                        )
                    )
                    
                    output = result.stdout.strip()
                    # 限制 ARP 命令输出的日志，避免过多冗余信息
                    if len(cmd) > 2 and cmd[2] == "show" and len(cmd) == 3:
                        # 对于 "ip neigh show" 命令（查看所有 ARP 条目），只输出与当前 IP 相关的内容
                        relevant_lines = []
                        for line in output.split('\n'):
                            if ip_address in line:
                                relevant_lines.append(line)
                        if relevant_lines:
                            filtered_output = '\n'.join(relevant_lines)
                            _LOGGER.debug("Linux ARP 命令 %s 输出（过滤后）: %s", cmd, filtered_output)
                        else:
                            _LOGGER.debug("Linux ARP 命令 %s 输出: 无相关条目", cmd)
                    else:
                        # 对于其他命令，正常输出
                        _LOGGER.debug("Linux ARP 命令 %s 输出: %s", cmd, output)
                    
                    if output:
                        # 检查是否有有效的 ARP 条目
                        lines = output.split('\n')
                        has_valid_entry = False
                        for line in lines:
                            line = line.strip()
                            if ip_address in line:
                                # 检查是否为 FAILED 状态
                                if "FAILED" in line.upper():
                                    # _LOGGER.debug("ARP 表检测: %s (设备: %s) 存在 FAILED 条目（视为离线）: %s", 
                                    #             ip_address, device_name or ip_address, line)
                                    continue
                                
                                # 检查是否为 STALE 状态
                                if "STALE" in line.upper():
                                    # _LOGGER.info("ARP 表检测: %s (设备: %s) 存在 STALE 条目（已过期，视为离线）: %s", 
                                    #             ip_address, device_name or ip_address, line)
                                    continue
                                
                                # 检查是否为其他有效状态
                                valid_states = ["REACHABLE", "DELAY", "PROBE"]
                                valid_state_found = False
                                for state in valid_states:
                                    if state in line.upper():
                                        _LOGGER.info("ARP 表检测: %s (设备: %s) 存在有效条目 (状态: %s): %s", 
                                                    ip_address, device_name or ip_address, state, line)
                                        has_valid_entry = True
                                        valid_state_found = True
                                        break
                                
                                # 如果没有找到有效状态，视为离线
                                if not valid_state_found:
                                    # _LOGGER.debug("ARP 表检测: %s (设备: %s) 存在未知状态条目，视为离线: %s", ip_address, device_name or ip_address, line)
                                    continue
                        
                        # 检查是否有有效条目
                        if has_valid_entry:
                            return True
                        # 如果找到相关条目但都是无效状态，继续尝试下一个命令
                        elif any(ip_address in line for line in output.split('\n')):
                            # 找到相关条目但都是无效状态，继续尝试下一个命令
                            _LOGGER.debug("ARP 表检测: %s (设备: %s) 找到相关条目但状态无效，继续尝试下一个命令", 
                                        ip_address, device_name or ip_address)
                except Exception as cmd_err:
                    _LOGGER.debug("ARP 命令执行失败: %s", cmd_err)
        elif system == "darwin":
            # macOS: 使用 arp -n
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["arp", "-n", ip_address],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
            )
            output = result.stdout.strip()
            # 检查是否有有效的 MAC 地址（不是 incomplete）
            if output and ip_address in output and "no entry" not in output.lower():
                if "incomplete" not in output.lower():
                    _LOGGER.debug("ARP 表检测: %s (设备: %s) 存在有效条目: %s", ip_address, device_name or ip_address, output)
                    return True
        else:
            # Windows 或其他系统: 使用 arp -a
            # 尝试多种 ARP 表查看命令
            arp_commands = [
                ["arp", "-a", ip_address],  # 查看指定 IP
                ["arp", "-a"]  # 查看所有 ARP 条目
            ]
            
            for cmd in arp_commands:
                try:
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: subprocess.run(
                            cmd,
                            capture_output=True,
                            text=True,
                            timeout=5
                        )
                    )
                    
                    output = result.stdout.strip()
                    _LOGGER.debug("Windows ARP 命令 %s 输出: %s", cmd, output[:200])  # 限制输出长度
                    
                    # 检查是否有有效的 ARP 条目
                    if output:
                        # 在 Windows 上，ARP 表格式类似：
                        #   接口: 192.168.1.1
                        #     Internet 地址         物理地址              类型
                        #     192.168.1.2           aa-bb-cc-dd-ee-ff     动态
                        lines = output.split('\n')
                        has_valid_entry = False
                        for line in lines:
                            line = line.strip()
                            if ip_address in line:
                                # 检查是否有 MAC 地址（包含连字符或冒号）
                                if '-' in line or ':' in line:
                                    # 排除无效条目
                                    if 'invalid' not in line.lower():
                                        _LOGGER.debug("ARP 表检测: %s (设备: %s) 存在有效条目: %s", ip_address, device_name or ip_address, line)
                                        has_valid_entry = True
                                        break
                        
                        # 检查是否有有效条目
                        if has_valid_entry:
                            return True
                        # 如果找到相关条目但都是无效状态，继续尝试下一个命令
                        elif any(ip_address in line for line in output.split('\n')):
                            # 找到相关条目但都是无效状态，继续尝试下一个命令
                            _LOGGER.debug("ARP 表检测: %s (设备: %s) 找到相关条目但状态无效，继续尝试下一个命令", 
                                        ip_address, device_name or ip_address)
                except Exception as cmd_err:
                    _LOGGER.debug("ARP 命令执行失败: %s", cmd_err)
        
        _LOGGER.debug("ARP 表检测: %s (设备: %s) 无有效条目", ip_address, device_name or ip_address)
        return False
    except Exception as err:
        _LOGGER.error("检查 ARP 表时出错: %s", err)
        return False

# 接口缓存和命令状态记录
ARPING_CACHE = {
    'working_interface': None,  # 记录最近成功的网络接口
    'last_check': 0  # 上次检查时间戳
}

async def arping(ip_address: str, count: int = 3, timeout: float = 2.0, device_name: str = None) -> bool:
    """使用 arping 发送 ARP 请求检测设备是否在线
    
    ARP 请求的优势：即使设备锁屏或进入省电模式，只要连接到网络就必须响应 ARP
    
    Args:
        ip_address: 要检测的 IP 地址
        count: 发送的 ARP 请求数量
        timeout: 超时时间（秒）
        device_name: 设备名称，用于日志
        
    Returns:
        bool: 设备是否响应了 ARP 请求
    """
    import platform
    import shutil
    import time
    system = platform.system().lower()
    
    try:
        # 首先检查 arping 是否可用
        arping_path = shutil.which("arping")
        
        if arping_path:
            # 使用 arping 命令
            # 自动检测可用的网络接口
            def get_available_interfaces():
                """获取可用的网络接口列表"""
                try:
                    import netifaces
                    interfaces = netifaces.interfaces()
                    # 过滤掉回环接口
                    return [iface for iface in interfaces if iface != 'lo']
                except:
                    # 备选方案：使用 ip link show
                    try:
                        result = subprocess.run(
                            ["ip", "link", "show"],
                            capture_output=True,
                            text=True,
                            timeout=5
                        )
                        interfaces = []
                        for line in result.stdout.split('\n'):
                            if ': <' in line and 'lo:' not in line:
                                iface = line.split(':')[1].strip()
                                if iface and not iface.startswith('@'):
                                    interfaces.append(iface)
                        return interfaces
                    except:
                        return []
            
            available_interfaces = get_available_interfaces()
            # 减少日志输出，避免定时检测时日志过多
            # _LOGGER.debug("可用的网络接口: %s", available_interfaces)
            
            # 构建 arping 命令列表
            arping_commands = []
            
            # 1. 优先使用最近成功的接口
            if ARPING_CACHE['working_interface'] and ARPING_CACHE['working_interface'] in available_interfaces:
                iface = ARPING_CACHE['working_interface']
                iface_cmd = ["arping", "-I", iface, "-c", str(count), "-w", str(int(timeout * 1000)), ip_address]
                arping_commands.append(iface_cmd)
            
            # 2. 使用基础命令（自动选择接口）
            if system == "linux":
                base_cmd = ["arping", "-c", str(count), "-w", str(int(timeout * 1000)), ip_address]
            elif system == "darwin":
                base_cmd = ["arping", "-c", str(count), "-w", str(int(timeout * 1000)), ip_address]
            else:
                base_cmd = ["arping", "-c", str(count), ip_address]
            arping_commands.append(base_cmd)
            
            # 3. 如果有其他可用接口，尝试第一个
            if available_interfaces:
                first_iface = available_interfaces[0]
                if first_iface != ARPING_CACHE['working_interface']:
                    iface_cmd = ["arping", "-I", first_iface, "-c", str(count), "-w", str(int(timeout * 1000)), ip_address]
                    arping_commands.append(iface_cmd)
            
            # 限制命令数量，避免过多尝试
            arping_commands = arping_commands[:3]  # 最多尝试3个命令
            
            # 尝试 arping 命令
            is_online = False
            working_iface = None
            
            for cmd_idx, cmd in enumerate(arping_commands):
                try:
                    # 减少日志输出
                    # _LOGGER.debug("尝试 arping 命令 %d/%d: %s", cmd_idx + 1, len(arping_commands), cmd)
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: subprocess.run(
                            cmd,
                            capture_output=True,
                            text=True,
                            timeout=timeout + 1
                        )
                    )
                    
                    # 检查是否收到响应
                    cmd_online = result.returncode == 0 or "reply" in result.stdout.lower()
                    # 减少日志输出
                    # _LOGGER.debug("arping 命令 %s 结果: %s (returncode=%d)", 
                    #              cmd, "在线" if cmd_online else "离线", result.returncode)
                    
                    if cmd_online:
                        is_online = True
                        
                        # 第一次成功的命令打印日志
                        if cmd_idx == 0:
                            _LOGGER.debug("arping 命令第一次尝试成功: %s", cmd)
                        
                        # 记录工作的接口
                        if "-I" in cmd:
                            iface_idx = cmd.index("-I") + 1
                            if iface_idx < len(cmd):
                                working_iface = cmd[iface_idx]
                                ARPING_CACHE['working_interface'] = working_iface
                        
                        break
                except Exception:
                    # 静默处理错误，避免日志过多
                    pass
            
            # 如果 arping 失败，尝试直接检查 ARP 表
            if not is_online:
                # 减少日志输出
                # _LOGGER.debug("arping 命令失败，尝试直接检查 ARP 表")
                arp_table_result = await check_arp_table(ip_address, device_name=device_name)
                if arp_table_result:
                    # 减少日志输出
                    # _LOGGER.debug("ARP 表检查成功，确认设备 %s 在线", ip_address)
                    is_online = True
            
            # 更新检查时间
            ARPING_CACHE['last_check'] = time.time()
            
            return is_online
        else:
            # arping 不可用，使用主动触发 ARP 的方式
            _LOGGER.debug("arping 不可用，使用 socket 触发 ARP 方式检测 %s", ip_address)
            
            # 使用多种方式尝试触发 ARP 表更新
            # 方式1: 尝试建立 TCP 连接（会触发 ARP 请求，即使端口关闭）
            # 方式2: 发送 UDP 包（同样会触发 ARP）
            # 方式3: ping（作为备选）
            
            async def trigger_arp_via_socket():
                """通过 socket 连接触发 ARP 请求"""
                import socket
                
                def _socket_trigger():
                    # 尝试多个常见端口
                    for port in [80, 443, 7, 22, 53]:
                        try:
                            # TCP 连接尝试（即使失败也会触发 ARP）
                            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            sock.settimeout(0.5)
                            sock.connect_ex((ip_address, port))
                            sock.close()
                            return True
                        except:
                            pass
                        
                        try:
                            # UDP 发送（同样会触发 ARP）
                            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                            sock.settimeout(0.5)
                            sock.sendto(b'', (ip_address, port))
                            sock.close()
                            return True
                        except:
                            pass
                    return False
                
                await asyncio.get_event_loop().run_in_executor(None, _socket_trigger)
            
            # 触发 ARP
            await trigger_arp_via_socket()
            
            # 同时发送 ping 作为备选触发方式
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: subprocess.run(
                        ["ping", "-c", "1", "-W", "1", ip_address],
                        capture_output=True,
                        timeout=2
                    )
                )
            except:
                pass
            
            # 等待 ARP 表更新
            await asyncio.sleep(0.3)
            
            # 检查 ARP 表
            return await check_arp_table(ip_address, device_name=device_name)
            
    except asyncio.TimeoutError:
        _LOGGER.debug("arping %s 超时", ip_address)
        return False
    except Exception as err:
        _LOGGER.error("arping 检测时出错: %s", err)
        return False

async def check_device_status(
    host: str, 
    ping_count: int = DEFAULT_PING_COUNT,
    detection_method: str = DEFAULT_DETECTION_METHOD,
    device_name: str = None  # 设备名称，用于日志
) -> Tuple[bool, datetime]:
    """检查设备在线状态
    
    Args:
        host: 设备主机地址或MAC地址（格式：xx:xx:xx:xx:xx:xx）
        ping_count: 发送的ping包数量，任意一个成功即判定在线
        detection_method: 检测方式 (ping/arp/auto)
        device_name: 设备名称，用于日志
        
    Returns:
        Tuple[bool, datetime]: 返回设备是否在线和检查时间
    """
    try:
        current_time = datetime.now()
        ip_address = host
        
        # 检查是否为MAC地址，如果是则转换为 IP
        if ":" in host and host.count(":") >= 2:
            ip_address = get_ip_from_mac(host)
            if not ip_address:
                _LOGGER.warning("无法获取MAC地址 %s 对应的IP地址", host)
                return False, current_time
            _LOGGER.debug("MAC地址 %s 对应IP: %s", host, ip_address)
        
        is_online = False
        
        if detection_method == DETECTION_PING:
            # 仅使用 ICMP ping
            is_online = await _ping_check(ip_address, ping_count, device_name=device_name)
            
        elif detection_method == DETECTION_ARP:
            # 仅使用 ARP 检测（适合移动设备）
            # 减少日志输出
            # _LOGGER.info("开始 ARP 模式检测设备 %s", ip_address)
            
            # 1. 首先清理并刷新 ARP 表
            # 减少日志输出
            # _LOGGER.debug("尝试刷新 ARP 表以获取最新状态")
            await asyncio.sleep(0.2)
            
            # 2. 发送多次 ARP 请求以提高成功率
            arp_success = False
            for attempt in range(3):
                # 减少日志输出
                # _LOGGER.debug("ARP 检测尝试 %d/3 for %s", attempt + 1, ip_address)
                arp_result = await arping(ip_address, count=ping_count, device_name=device_name)
                if arp_result:
                    # 减少日志输出
                    # _LOGGER.info("ARP 检测尝试 %d/3 成功: %s", attempt + 1, ip_address)
                    arp_success = True
                    break
                await asyncio.sleep(0.5)
            
            is_online = arp_success
            
            # 3. ARP 失败时，详细检查 ARP 表
            if not is_online:
                # 减少日志输出
                # _LOGGER.warning("%s ARP 检测失败，开始详细排查", ip_address)
                
                # 直接检查 ARP 表
                import platform
                system = platform.system().lower()
                
                if system == "windows":
                    # Windows: 检查 ARP 表
                    try:
                        result = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: subprocess.run(
                                ["arp", "-a"],
                                capture_output=True,
                                text=True,
                                timeout=5
                            )
                        )
                        # 减少日志输出
                        # _LOGGER.debug("Windows 完整 ARP 表: %s", result.stdout[:500])
                    except Exception:
                        # 减少日志输出
                        # _LOGGER.debug("检查 Windows ARP 表失败: %s", e)
                        pass
                
                # 4. 尝试 ping 作为备选
                # 减少日志输出
                # _LOGGER.debug("%s ARP 检测失败，尝试 ping 作为备选", ip_address)
                ping_result = await _ping_check(ip_address, ping_count, device_name=device_name)
                if ping_result:
                    # 减少日志输出
                    # _LOGGER.info("%s ping 检测成功，确认在线", ip_address)
                    is_online = True
                else:
                    # 减少日志输出
                    # _LOGGER.debug("%s ping 检测也失败", ip_address)
                    pass
            
        elif detection_method == DETECTION_AUTO:
            # 自动模式：先尝试 ARP，失败则使用 ping
            # ARP 更适合移动设备，因为即使锁屏也能响应
            is_online = await arping(ip_address, count=ping_count, device_name=device_name)
            if not is_online:
                _LOGGER.debug("%s ARP 检测失败，尝试 ping 检测", ip_address)
                is_online = await _ping_check(ip_address, ping_count, device_name=device_name)
        else:
            # 默认使用 ARP + ping 组合检测
            is_online = await arping(ip_address, count=ping_count, device_name=device_name)
            if not is_online:
                _LOGGER.debug("%s ARP 检测失败，尝试 ping 作为备选", ip_address)
                ping_result = await _ping_check(ip_address, ping_count, device_name=device_name)
                if ping_result:
                    _LOGGER.debug("%s ping 检测成功，确认在线", ip_address)
                    is_online = True
        
        _LOGGER.debug("设备 %s (IP: %s) 检测结果: %s (方式: %s)", 
                     device_name or host, ip_address, "在线" if is_online else "离线", detection_method)
        return is_online, current_time
        
    except Exception as err:
        _LOGGER.error("检查设备状态时出错: %s", err)
        return False, datetime.now()

async def _ping_check(ip_address: str, ping_count: int, device_name: str = None) -> bool:
    """使用 ICMP ping 检测设备
    
    Args:
        ip_address: IP 地址
        ping_count: ping 包数量
        device_name: 设备名称，用于日志
        
    Returns:
        bool: 是否在线
    """
    try:
        host_ping = await async_ping(ip_address, count=ping_count, timeout=2, interval=0.5)
        is_online = host_ping.packets_received > 0
        _LOGGER.debug("ping 设备 %s (IP: %s): %s (收到 %d/%d 包)", 
                     device_name or ip_address, ip_address, "在线" if is_online else "离线",
                     host_ping.packets_received, ping_count)
        return is_online
    except Exception as err:
        _LOGGER.error("ping 检测失败: %s", err)
        return False

async def update_device_data(
    device_data: Dict[str, Any], 
    host: str, 
    store: Store, 
    entry_id: str,
    offline_threshold: int = DEFAULT_OFFLINE_THRESHOLD,
    ping_count: int = DEFAULT_PING_COUNT,
    retry_interval: float = DEFAULT_RETRY_INTERVAL,
    retry_ping_count: int = DEFAULT_RETRY_PING_COUNT,
    mode: str = MODE_PARALLEL,
    reset_fail_count: bool = False,  # 是否重置失败计数，用于API调用时立即判断状态
    detection_method: str = DEFAULT_DETECTION_METHOD  # 检测方式
) -> Dict[str, Any]:
    """更新设备数据（带防抖机制）
    
    支持两种检测模式：
    1. parallel（并行ping，默认）：单次发送多个ping包，快速返回
    2. retry（快速重试）：检测到离线时立即连续重试确认，更可靠
    
    支持三种检测方式：
    1. ping：仅使用 ICMP ping
    2. arp：仅使用 ARP 检测（适合移动设备）
    3. auto：先 ping，失败则 arp
    
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
        reset_fail_count: 是否重置失败计数，用于API调用时立即判断状态
        detection_method: 检测方式，"ping"、"arp" 或 "auto"
        
    Returns:
        Dict[str, Any]: 更新后的设备数据
    """
    try:
        # 每轮扫描开始时打印IP、MAC和名称的对应关系
        device_name = device_data['name']
        _LOGGER.debug("开始检测设备 %s (IP: %s)", device_name, host)
        
        is_online, current_time = await check_device_status(host, ping_count, detection_method, device_name=device_name)
        current_date = current_time.date()
        current_date = current_time.date()
        was_online = device_data.get("is_online", False)
        
        # 使用设备名称作为显示名称
        display_name = device_name
        # 初始化失败计数器
        if "fail_count" not in device_data:
            device_data["fail_count"] = 0
        
        # 保存原始失败计数，用于API调用后恢复
        original_fail_count = device_data["fail_count"]
        
        # API调用时使用临时失败计数，不影响定时任务的累积计数
        temp_fail_count = 0
        
        # 防抖逻辑
        if is_online:
            # 设备在线，重置失败计数
            device_data["fail_count"] = 0
            final_online_status = True
            # 减少日志输出
            # _LOGGER.debug("设备 %s 检测在线，重置失败计数",  display_name)
        else:
            # 设备检测离线
            if reset_fail_count:
                # API调用时使用临时计数，不影响原始计数
                temp_fail_count = 1
                # 减少日志输出
                # _LOGGER.debug("设备 %s 检测离线（API调用），临时失败计数: %d/%d, 模式: %s", 
                #              display_name, temp_fail_count, offline_threshold, mode)
            else:
                # 定时任务时累积原始计数
                device_data["fail_count"] += 1
                # 减少日志输出
                # _LOGGER.debug("设备 %s 检测离线，失败计数: %d/%d, 模式: %s", 
                #              display_name, device_data["fail_count"], offline_threshold, mode)
            
            # 快速重试模式：检测到离线时立即连续重试
            if mode == MODE_RETRY and (was_online or reset_fail_count):
                # 计算当前使用的失败计数
                current_fail_count = temp_fail_count if reset_fail_count else device_data["fail_count"]
                if current_fail_count < offline_threshold:
                    # 只在状态变化时输出日志
                    if was_online:
                        _LOGGER.info("设备 %s 从在线变为离线，开始快速重试确认", display_name)
                    
                    retry_success = False
                    for retry in range(offline_threshold - current_fail_count):
                        await asyncio.sleep(retry_interval)
                        retry_online, _ = await check_device_status(host, retry_ping_count, detection_method)
                        
                        if retry_online:
                            if reset_fail_count:
                                # API调用时，重试成功则保持在线状态
                                final_online_status = True
                                # 只在状态变化时输出日志
                                if was_online:
                                    _LOGGER.info("设备 %s 快速重试成功，确认在线", display_name)
                            else:
                                # 定时任务时，重试成功则重置原始计数
                                device_data["fail_count"] = 0
                                final_online_status = True
                                # 只在状态变化时输出日志
                                if was_online:
                                    _LOGGER.info("设备 %s 快速重试成功，确认在线", display_name)
                            retry_success = True
                            break
                        else:
                            if reset_fail_count:
                                # API调用时，使用临时计数
                                temp_fail_count += 1
                                # 减少日志输出
                                # _LOGGER.debug("设备 %s 快速重试第 %d 次失败，临时失败计数: %d/%d", 
                                #              display_name, retry + 1, temp_fail_count, offline_threshold)
                            else:
                                # 定时任务时，累积原始计数
                                device_data["fail_count"] += 1
                                # 减少日志输出
                                # _LOGGER.debug("设备 %s 快速重试第 %d 次失败，失败计数: %d/%d", 
                                #              display_name, retry + 1, device_data["fail_count"], offline_threshold)
                    
                    if retry_success:
                        # 重试成功，已经设置了final_online_status，直接进入下一阶段
                        pass
                    else:
                        # 重试失败，判断最终状态
                        if reset_fail_count:
                            # API调用时，使用临时计数判断
                            if temp_fail_count >= offline_threshold:
                                final_online_status = False
                                # 只在状态变化时输出日志
                                if was_online:
                                    _LOGGER.info("设备 %s API调用确认离线", display_name)
                            else:
                                # API调用时，如果重试失败次数未达阈值，立即返回离线
                                final_online_status = False
                                # 减少日志输出
                                # _LOGGER.debug("设备 %s API调用重试失败次数未达阈值，但立即返回离线状态", display_name)
                        else:
                            # 定时任务时，使用原始计数判断
                            if device_data["fail_count"] >= offline_threshold:
                                final_online_status = False
                                # 只在状态变化时输出日志
                                if was_online:
                                    _LOGGER.info("设备 %s 确认离线", display_name)
                            else:
                                # 还没达到阈值，保持之前的在线状态
                                final_online_status = was_online
                                # 减少日志输出
                                # _LOGGER.debug("设备 %s 失败次数未达阈值，保持状态: %s", display_name, "在线" if final_online_status else "离线")
            else:
                # 非快速重试模式，直接判断状态
                if reset_fail_count:
                    # API调用时，只要检测失败，立即返回离线
                    final_online_status = False
                    # 减少日志输出
                    # _LOGGER.debug("设备 %s API调用检测离线，立即返回离线状态", display_name)
                else:
                    # 定时任务时，使用原始计数判断
                    if device_data["fail_count"] >= offline_threshold:
                        final_online_status = False
                        # 只在状态变化时输出日志
                        if was_online:
                            _LOGGER.info("设备 %s 确认离线", display_name)
                    else:
                        # 还没达到阈值，保持之前的在线状态
                        final_online_status = was_online
                        # 减少日志输出
                        # _LOGGER.debug("设备 %s 失败次数未达阈值，保持状态: %s", display_name, "在线" if final_online_status else "离线")
        
        # 如果是新的一天，重置计时
        if current_date != device_data["last_date"]:
            device_data["online_time"] = 0
            device_data["last_date"] = current_date
        
        # 计算在线时间
        if device_data.get("last_check") and device_data.get("is_online") and final_online_status:
            time_diff = (current_time - device_data["last_check"]).total_seconds() / 60
            device_data["online_time"] += time_diff
        
        # 只在状态变化时输出日志
        if was_online != final_online_status:
            if final_online_status:
                _LOGGER.info("设备 %s 恢复在线", display_name)
            else:
                _LOGGER.info("设备 %s 变为离线", display_name)
        
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
    detection_method = entry.options.get(
        CONF_DETECTION_METHOD,
        entry.data.get(CONF_DETECTION_METHOD, DEFAULT_DETECTION_METHOD)
    )
    
    _LOGGER.info(
        "设备 %s 配置: enabled=%s, scan_interval=%ds, offline_threshold=%d, ping_count=%d, retry_interval=%ds, retry_ping_count=%d, detection_method=%s",
        device_name, enabled, scan_interval, offline_threshold, ping_count, retry_interval, retry_ping_count, detection_method
    )
    
    # 创建存储对象
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    
    # 初始化设备数据
    device_data = {
        "name": device_name,
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
        "detection_method": detection_method,
    }
    
    unique_id = f"{entry.entry_id}_status"
    entity_id = f"binary_sensor.{device_name.lower().replace(' ', '_')}_status"
    
    async def async_update_data():
        """Fetch data from API endpoint."""
        # 每次检测时读取最新的enabled状态
        latest_config = hass.data.get(f"{DOMAIN}_config", {}).get(entry.entry_id, {})
        latest_enabled = latest_config.get("enabled", DEFAULT_ENABLED)
        
        # 检查设备是否启用
        if not latest_enabled:
            _LOGGER.debug("设备 %s 已禁用，跳过定时检测", device_name)
            return device_data
        
        # 使用最新的配置参数
        latest_offline_threshold = latest_config.get("offline_threshold", offline_threshold)
        latest_ping_count = latest_config.get("ping_count", ping_count)
        latest_retry_interval = latest_config.get("retry_interval", retry_interval)
        latest_retry_ping_count = latest_config.get("retry_ping_count", retry_ping_count)
        latest_detection_method = latest_config.get("detection_method", detection_method)
        
        return await update_device_data(
            device_data, host, store, entry.entry_id,
            offline_threshold=latest_offline_threshold,
            ping_count=latest_ping_count,
            retry_interval=latest_retry_interval,
            retry_ping_count=latest_retry_ping_count,
            mode=MODE_RETRY,  # 定时扫描默认使用快速重试模式，提高检测可靠性
            detection_method=latest_detection_method
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

    # 注册配置更新回调
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    return True

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reload a config entry."""
    # 先卸载
    await async_unload_entry(hass, entry)
    # 重新加载
    return await async_setup_entry(hass, entry)

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    # 当选项更新时，重新加载配置
    await async_reload_entry(hass, entry)

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