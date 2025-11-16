这是一个home assistant设备在线检测插件，统计每日在线时长及在线状态。

# 教程
基本使用 https://www.bilibili.com/video/BV1vHoTYcE85
## 功能
支持ip、域名、Mac地址（xx:xx:xx:xx:xx:xx）

# 安装
## 方法一：
1. 首先确保已安装HACS
2. 在HACS中添加自定义仓库https://github.com/qq273681448/ha-DeviceOnlineTracker
3. 搜索"Device Online Tracker"并安装
4. 刷新页面
## 方法二（暂不可用）

# 预览
![Alt text](img/image.png)

# 手动触发检查

## 服务

### ping_all

**服务名称**: `device_online_tracker.ping_all`

**功能**: 手动触发所有已配置设备的在线状态检查

**调用方式**: 可通过Home Assistant的开发者工具、自动化、脚本或API调用

### ping_device

**服务名称**: `device_online_tracker.ping_device`

**功能**: 手动触发单个设备的在线状态检查

**参数**:
- `device_name` (可选): 设备名称（支持模糊匹配）
- `entry_id` (可选): 设备的配置条目ID

**调用方式**: 可通过Home Assistant的开发者工具、自动化、脚本或API调用

## API调用示例

### 使用REST API调用ping_all服务
```bash
curl -X POST http://your-homeassistant-ip:8123/api/services/device_online_tracker/ping_all \
  -H "Authorization: Bearer YOUR_LONG_LIVED_TOKEN" \
  -H "Content-Type: application/json"
```

### 使用REST API调用ping_device服务（按设备名称）
```bash
curl -X POST http://your-homeassistant-ip:8123/api/services/device_online_tracker/ping_device \
  -H "Authorization: Bearer YOUR_LONG_LIVED_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"device_name": "My Phone"}'
```

### 使用REST API调用ping_device服务（按entry_id）
```bash
curl -X POST http://your-homeassistant-ip:8123/api/services/device_online_tracker/ping_device \
  -H "Authorization: Bearer YOUR_LONG_LIVED_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"entry_id": "YOUR_DEVICE_ENTRY_ID"}'
```

## 自动化示例

### 定时触发所有设备检查
```yaml
alias: 每日早晨8点检查所有设备
trigger:
  - platform: time
    at: "08:00:00"
action:
  - service: device_online_tracker.ping_all
```

### 按钮触发设备检查
```yaml
alias: 点击按钮检查手机状态
trigger:
  - platform: device
    device_id: YOUR_BUTTON_DEVICE_ID
    domain: device
    type: action
    subtype: single_press
action:
  - service: device_online_tracker.ping_device
    data:
      device_name: "My Phone"}
```

# 注意事项

1. 服务调用需要Home Assistant 2021.10或更高版本
2. API调用需要使用长期访问令牌
3. 设备名称匹配不区分大小写
4. 支持同时使用多个参数进行更精确的匹配