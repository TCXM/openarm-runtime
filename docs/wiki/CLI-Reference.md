# 命令行参考

适用版本：**v0.6.0**。以下命令在连接硬件的 **Linux 工作站**及已安装相应 extras 的 Python 3.12 环境内执行。Mac／Windows 远程访问命令见[远程访问](Remote-Access.md)。

## 入口与参数位置

```sh
openarm-runtime --help
python -m openarm_runtime --help
openarm-runtime platform --help
```

两个入口等价。`--help` 只显示帮助，不打开设备。`--config` 是全局参数，必须放在子命令前；`--profile` 是 `platform` 或 `video` 自己的参数。

```sh
openarm-runtime --config ~/.config/openarm/classic.json check
openarm-runtime platform --profile ~/.config/openarm-runtime/platform.json
```

将 `YOUR_*`、`/path/to/...` 替换为本机值。配置文件的用途见[安装与配置](Installation.md)。

| 命令 | 用途 | 运行时对硬件的影响 |
| --- | --- | --- |
| `platform` | 统一网页入口 | 按配置启动／复用服务；不自动使能电机或开始跟随 |
| `prepare` | 下载并校验固定版本的模型资源 | 不打开硬件 |
| `check` | 双 CAN 总线和电机只读检查 | 打开总线并查询；不发送运动控制 |
| `view` | 从臂反馈三维显示 | 只读 CAN |
| `panel` | 手动控制及自动标定面板 | 启动时只读；操作按钮可使能、移动、写零位 |
| `leader` | 主臂读取、软件标定、姿态预览 | 读取主臂串口；加 `--follow` 后提供人工启动的真实跟随 |
| `calibrate` | 单臂自动硬件零位标定 | **执行命令即进入运动及写零位流程** |
| `video` | RGB／深度画面 | 真实配置打开相机；`--simulate` 仅生成软件图像 |
| `tactile` | 触觉采集和可视化 | 真实配置打开串口；`--simulate` 不打开硬件 |

v0.6.0 没有独立的 `hold` 子命令。手动保持用 `panel`；程序集成使用 [`hold_arm`](Python-API.md)。

## platform：统一网页

日常使用只启动这一项：

```sh
openarm-runtime platform --profile ~/.config/openarm-runtime/platform.json
```

在工作站浏览器打开 **http://127.0.0.1:8888**。已有开发环境也可在仓库根目录执行 `python3 run_platform.py`；该脚本使用仓库 `.venv` 和上述默认配置路径。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--profile PATH` | 不加载文件 | 指定服务命令、端口及日志位置的外部 JSON |
| `--port PORT` | 配置值，未配置时 `8888` | 覆盖浏览器入口端口 |
| `--attach-only` | 关闭 | 只连接现有服务，不启动或停止进程 |

```sh
# 只查看已经运行的服务
openarm-runtime platform --profile ~/.config/openarm-runtime/platform.json --attach-only

# 入口换到 8889；远程 SSH 转发的目标端口也要对应修改
openarm-runtime platform --profile ~/.config/openarm-runtime/platform.json --port 8889
```

不传 `--profile` 时只有默认端口映射，没有自动发现设备或推断启动命令的能力。内部模块仍使用独立进程与端口，浏览器只需访问统一入口。

## prepare：模型资源

```sh
openarm-runtime prepare --asset-root ~/.cache/openarm-models
```

`--asset-root PATH` 必填。命令需要网络下载固定来源的模型并验证内容；资源保存在仓库外。`view`、`panel`、`leader` 的 `--asset-root` 指向同一位置。

## check：只读 CAN 检查

```sh
openarm-runtime --config ~/.config/openarm/classic.json check
```

无额外参数。输出 JSON，包含总检查结果、左右映射确认标志和各总线检查信息；通过时退出码为 `0`，失败为 `1`。该命令要求电机处于失能状态。先停止占用总线的控制操作，再检查。

`check` 虽然只读，仍会检查原生 SDK 编码器，须安装 `follower` extra 和 OpenArm CAN SDK 1.4.1；它不发送编码后的控制帧。仅需 core 的 `ArmConnection` 只读遥测是另一个接口，见 [Python API](Python-API.md)。

它检查健康的 Classic CAN、1 Mbps、FD 关闭、`restart-ms 0`，不自动修改链路。`mapping_verified` 仍需操作人员根据接线确认；检查通过不等于左右机械映射已确认。

## view 与 panel：从臂查看和控制

```sh
# 单独查看从臂反馈
openarm-runtime --config ~/.config/openarm/classic.json view \
  --asset-root ~/.cache/openarm-models --port 8890

# 单独打开控制面板（不要与平台管理的同一模块同时运行）
openarm-runtime --config ~/.config/openarm/classic.json panel \
  --asset-root ~/.cache/openarm-models \
  --report-root ~/openarm-reports --port 8893
```

| 参数 | `view` | `panel` |
| --- | --- | --- |
| 全局 `--config PATH` | 必填 | 必填 |
| `--asset-root PATH` | 必填 | 必填 |
| `--port PORT` | 默认 `8890` | CLI 默认 `8890`；平台示例显式使用 `8893` |
| `--report-root PATH` | 不支持 | 必填，保存控制／标定报告 |

`panel` 启动时只读，使能前关节偏移滑条不可操作。执行“使能并保持”后才保持当前姿态并响应偏移滑条。页面中的“自动标定”会运行硬件零位标定。操作步骤见[操作指南](Operations.md)。

## leader：主臂预览与真实跟随

只读取主臂、进行软件标定与虚拟姿态预览：

```sh
openarm-runtime leader \
  --left-port /dev/serial/by-id/YOUR_LEFT_LEADER \
  --right-port /dev/serial/by-id/YOUR_RIGHT_LEADER \
  --calibration ~/.config/openarm/leader.json \
  --asset-root ~/.cache/openarm-models --port 8890
```

提供人工启动的真实跟随功能：

```sh
openarm-runtime --config ~/.config/openarm/classic.json leader \
  --left-port /dev/serial/by-id/YOUR_LEFT_LEADER \
  --right-port /dev/serial/by-id/YOUR_RIGHT_LEADER \
  --calibration ~/.config/openarm/leader.json \
  --asset-root ~/.cache/openarm-models \
  --follow --report-root ~/openarm-reports --port 8890
```

| 参数 | 要求／默认值 |
| --- | --- |
| `--left-port PATH`、`--right-port PATH` | 均必填；两个主臂串口的稳定设备路径 |
| `--calibration PATH` | 必填；主臂自然零位和夹爪端点的软件标定文件 |
| `--asset-root PATH` | 必填；已准备的模型目录 |
| `--port PORT` | 默认 `8890` |
| `--follow` | 默认关闭；加入真实从臂跟随控件，**不会自动开始跟随** |
| `--report-root PATH` | 使用 `--follow` 时必填 |
| 全局 `--config PATH` | 使用 `--follow` 时必填，位于 `leader` 前 |

主臂读取不会写舵机寄存器。主臂软件标定保存在 JSON，与从臂硬件零位标定是两种操作。跟随需要完整且保存的标定、有效反馈和可独占的从臂总线。

## calibrate：从臂硬件零位

**这是运动命令，不用于测试安装是否成功。** 先完成接线确认、参考姿态、卸载负载、清空运动范围及失能后的支撑准备，再停止所选总线上的读者／控制器。

```sh
openarm-runtime --config ~/.config/openarm/classic.json calibrate \
  --side right --report-root ~/openarm-reports
```

| 参数 | 要求／默认值 |
| --- | --- |
| 全局 `--config PATH` | 必填 |
| `--side left\|right` | 必填，一次一臂 |
| `--report-root PATH` | 必填 |
| `--allow-unreadable-mode` | 默认关闭；仅明确允许已验证硬件的 RID10 读取超时 |

成功流程包含寻止挡、回计算零位、失能、写入并验证 8 个电机零位。按 `Ctrl+C` 请求取消和失能。部分写入后发生错误不会自动回滚；检查报告及实际读回再决定后续处理。可读出的非 MIT 模式即使加该选项也会被拒绝。

完整步骤、执行边界和恢复要求见[硬件标定说明](https://github.com/TCXM/openarm-runtime/blob/v0.6.0/docs/calibration.md)。

## video：视觉与模拟

```sh
# 实机：外部配置包含头部、深度和腕部相机
openarm-runtime video --profile ~/.config/openarm/video-runtime.json --port 5003

# 离线：不打开相机
openarm-runtime video --simulate --port 5004
```

| 参数 | 默认值／说明 |
| --- | --- |
| `--profile PATH` | 外部相机配置；未使用模拟时必填 |
| `--simulate` | 生成软件 RGB／深度画面；可配合配置文件选择画面布局 |
| `--port PORT` | 默认 `5003` |

离线独立页为 http://127.0.0.1:5004。平台已管理视觉服务时，使用统一入口中的“视觉”，无需另启同一组设备。

## tactile：触觉与双传感器

```sh
# 单个真实传感器：device-id 需匹配该传感器，已验证设备使用 2
openarm-runtime tactile --device /dev/serial/by-id/YOUR_TACTILE \
  --device-id 2 --port 8891

# 两个真实传感器同页显示
openarm-runtime tactile \
  --device /dev/serial/by-id/YOUR_TACTILE_A \
  --right-device /dev/serial/by-id/YOUR_TACTILE_B \
  --device-id 2 --port 8891

# 单个模拟传感器，不打开串口
openarm-runtime tactile --simulate --port 8892
```

| 参数 | 默认值／约束 |
| --- | --- |
| `--device PATH` 或 `--simulate` | 必选其一，互斥 |
| `--right-device PATH` | 第二个传感器，必须配合 `--device`，且路径不同 |
| `--port PORT` | 默认 `8891`；双传感器时最大为 `65524` |
| `--baudrate BAUD` | 默认 `921600`，必须为正整数 |
| `--device-id ID` | 默认 `1`，范围 `1–255`；是通信地址，不是串口编号 |

`--right-device` 是保留的参数名，表示并排显示的第二路。本项目已验证安装中，**两路触觉均属于左臂，右臂没有触觉传感器**。双路共用命令中的波特率和设备地址。

双路独立服务还使用主端口 `+10/+11`，默认是 `8901/8902`，需同时空闲；通过平台访问时这些连接全部由 `8888` 转发。力值为原始 counts，未标定为牛顿。

## 端口与停止

网页端口参数范围为 `1024–65535`，平台端口、各模块及嵌套视图端口必须互不冲突。`Ctrl+C` 或 `SIGTERM` 走正常清理流程；退出 SSH 转发或关闭浏览器不等于停止远端平台。

下一步：[操作指南](Operations.md) · [Mac／Windows 远程访问](Remote-Access.md) · [故障排查](Troubleshooting.md)

参数依据：[v0.6.0 CLI 实现](https://github.com/TCXM/openarm-runtime/blob/v0.6.0/src/openarm_runtime/__main__.py)。
