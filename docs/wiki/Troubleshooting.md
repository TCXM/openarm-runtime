# 故障排查

[返回首页](Home.md) · [安装与配置](Installation.md) · [日常操作](Operations.md) · [远程访问](Remote-Access.md)

先区分 **网页服务是否可达** 和 **硬件数据是否正常**。平台概览中的在线状态表示对应端口能连接；相机断开、串口超时或 CAN 异常时，网页本身仍可能在线。

## 快速定位

| 现象 | 优先检查 |
| --- | --- |
| 工作站也打不开 `8888` | 平台是否启动、环境依赖、入口端口占用 |
| 工作站能打开，Mac / Windows 不能 | SSH 是否连对工作站、转发是否存活、本地端口是否占用 |
| 平台打开但某个模块失败 | 该模块日志、启动命令、设备路径及依赖 |
| 电机提示 `Expected healthy Classic...` | CAN 当前状态与参数，不能直接判断为权限不足 |
| 左右腕无图，头部正常 | 外部视频配置中的设备路径、USB 拓扑、左右对应关系 |
| 触觉有旧数值但不更新 | `live / stale / error` 状态、设备 ID、串口占用 |
| 无法从电机控制切到遥操 | 原控制器是否由平台管理，旧服务是否正常退出 |

以下终端命令默认在 Linux 工作站执行。远程客户端应先 `ssh USER@WORKSTATION` 登录工作站；Mac / Windows 本地排查命令见 [远程访问](Remote-Access.md)。

## 1. 平台或模块打不开

```sh
ss -ltnp 'sport = :8888'
curl --fail --max-time 3 http://127.0.0.1:8888/api/status
ls -l ~/.local/state/openarm-platform/
tail -n 80 ~/.local/state/openarm-platform/control.log
tail -n 80 ~/.local/state/openarm-platform/teleop.log
tail -n 80 ~/.local/state/openarm-platform/tactile.log
tail -n 80 ~/.local/state/openarm-platform/vision.log
```

日志目录默认如上，也可由平台配置的 `log_dir` 指定。`/api/status` 仅适合诊断页面服务，不代表电机已失能，也不是同步传感器数据接口。

如果 `openarm-runtime` 找不到或缺依赖：

```sh
source ~/.venvs/openarm/bin/activate
command -v python
command -v openarm-runtime
python -c 'import openarm_runtime; print(openarm_runtime.__version__)'
python -m pip check
```

检查安装环境是否包含 `[platform]` 及失败子模块的 extra。开发启动器 `run_platform.py` 使用仓库 `.venv`，并不使用另一个发布版环境。两者不可混为同一个安装位置。

### 端口占用与重复服务

```sh
ss -ltnp 'sport = :8893'
ss -ltnp 'sport = :8890'
ss -ltnp 'sport = :8891'
ss -ltnp 'sport = :5003'
```

这些是默认内部端口；以平台配置为准。确认进程用户、命令及用途后，在原终端按 `Ctrl+C` 正常退出旧服务。平台不会停止外部启动的进程；由其他账号启动的服务应由其操作者停止。不要仅凭端口号误停正在工作的控制器。

外部电机控制器存在时，平台可复用页面，但会拒绝自动切走它。正常停止旧控制器后，由平台重新管理电机控制和遥操。不要删除 CAN 锁文件或强行终止运动进程来绕过所有权检查。

## 2. CAN 健康检查错误

错误示例：

```text
RuntimeError: Expected healthy Classic 1 Mbps, fd off, restart-ms 0; no automatic reconfiguration
```

这是链路前置检查未满足，并不等同于“没有 sudo 权限”。检查命令：

```sh
ip -details -statistics link show can0
ip -details -statistics link show can1
```

程序要求同时满足：

| 项目 | 期望值 |
| --- | --- |
| 管理标志 | 包含 `UP` |
| CAN 状态 | `ERROR-ACTIVE` |
| MTU | `16`，即 Classic CAN |
| bitrate | `1000000` |
| FD | 关闭 |
| restart-ms | `0` |
| 收发错误计数 | 全部为 `0` |

只看普通 `state UP / DOWN` 一行不够，应查看详细 CAN 参数。`ERROR-ACTIVE` 是正常的 CAN 错误管理状态名称，不表示当前发生了故障。

### 为什么变成 DOWN / STOPPED

重启、显式关闭接口、USB CAN 适配器或其 Hub 断开后重新枚举，都可能使接口回到未启用状态。重枚举还可能丢失原先的 bitrate 设置。查看内核日志，结合实际时间确认：

```sh
journalctl -k --since '30 minutes ago'
ip -details link show can0
ip -details link show can1
```

看到 USB disconnect 只说明设备发生过断开，不能单凭日志确定是拔线、供电、Hub、驱动还是软件操作。先恢复稳定连接，再核对物理左右臂映射。

### 人工启用接口

只在操作者确认目标接口、总线无活动控制器、已知配置与设备要求一致后执行。以下是人工维护命令，不是平台启动步骤或自动恢复逻辑。

若接口已保存 **Classic 1 Mbps、FD off、restart-ms 0** 配置，只需启用：

```sh
sudo ip link set dev can0 up
sudo ip link set dev can1 up
```

若 USB 重枚举后 bitrate 未配置，且接口已经是 DOWN，先由操作者明确设置已确认的参数：

```sh
sudo ip link set dev can0 type can bitrate 1000000 fd off restart-ms 0
sudo ip link set dev can1 type can bitrate 1000000 fd off restart-ms 0
sudo ip link set dev can0 up
sudo ip link set dev can1 up
```

然后重新检查详细状态。不要对一个正在使用或已经健康的总线反复 down/up；错误计数持续增加或出现 BUS-OFF 时，应查供电、线缆、终端电阻、设备波特率和总线负载，不能用自动重启掩盖原因。程序本身不会替你修改 CAN。

明确的 `Operation not permitted` 才指向该项操作的权限问题。sudo 密码应仅在工作站终端输入；普通网页访问和大多数运行命令不需要以 root 启动整个程序。

## 3. 左腕 / 右腕没有画面

```sh
v4l2-ctl --list-devices
ls -l /dev/v4l/by-id /dev/v4l/by-path
curl --fail --max-time 3 http://127.0.0.1:8888/apps/vision/status
```

检查每路的错误、帧计数和帧龄。重点核对 `~/.config/openarm/video-runtime.json`：

- `/dev/camera_wrist_*` 自定义别名可能指向旧 USB 拓扑；名称存在与否须按实际机器检查。
- 两个同型号相机可能报告相同序列号，导致 `/dev/v4l/by-id` 无法区分。可使用两个不同的 `by-path`，通常选择可采集节点 `video-index0`。
- `by-path` 比易变化的 `/dev/videoN` 更适合区分 USB 端口，但换 Hub 或物理插口后仍须重新核对。
- 根据实际画面确认左 / 右映射，不能根据枚举先后猜测。
- 相机可能被旧采集进程占用；先正常停止旧采集者，再由新服务打开设备。

修改外部配置后，正常重启对应视觉服务以重新加载。深度单独失败时，核对 `depth_helper` 是否存在、可执行，以及 OpenNI2 / Orbbec 库是否来自匹配版本；它与 RGB UVC 路径不同。

## 4. 触觉无响应或保留旧数据

```sh
ls -l /dev/serial/by-id/
id
```

确认配置中的串口路径存在，并且运行账号有设备访问权限。USB 重新插拔可能使临时 ACL 失效；持久权限应通过针对实际设备的系统用户组或 udev 规则管理，不应将全部串口改成所有人可写。

检查 `--device-id` 与实际模块设置。已验证的设备地址是 **2**，CLI 默认是 **1**；设备地址与 `/dev/ttyACM0` 中的数字没有关系。已验证波特率为 **921600**。不要把所有超时都归因于波特率，更不要在排查时让多个采集者同时打开一个传感器。

页面保留最后读数不代表数据仍有效：`stale`、`error` 或“已失效”状态都不能作为实时样本。故障后触觉采集关闭串口，需要点击“重新连接”；不会自动切为模拟。暂停只冻结显示，不等于停止采集。

原始力单位为 counts；没有按传感器标定，不能将数值直接当作 N。当前两个传感器都属于左臂，页面左右顺序与机械臂左右是不同含义。

## 5. 主臂预览正常，但不能实体跟随

检查外部 `leader.json` 是否包含对应设备的有效标定，主臂串口路径是否正确，从臂配置中的物理映射是否经过确认。实体跟随还要求从臂总线没有其他读写者占用。

新鲜、有效反馈中的单独低压 / 电压告警按本项目已确认规则不阻断跟随；其他或组合错误、过期 / 损坏反馈、主臂已使能等仍会拒绝跟随。不能为了消除提示而伪造标定值或屏蔽其他故障。具体操作见 [操作指南](Operations.md)。

## 6. 退出、切换或清理超时

关闭浏览器、切换到视觉 / 触觉页面，或断开 SSH 转发，都不等于停止远端运动或服务。先使用运动页面的停止 / 失能操作，再按正常流程关闭服务。

平台给自有运动服务最多 40 秒进行正常退出；超时或异常退出会阻止模式切换，不会强杀运动进程。保留日志并确认硬件状态后再处理。相机驱动阻塞、串口或 CAN 清理失败也应视为未确认正常关闭，不能仅凭网页已消失判断资源已释放。

向维护者反馈时提供版本、执行命令（去除私有路径和身份信息）、错误全文、相关时间段日志与设备状态。不要上传标定、凭据、整份机器配置或未经处理的原始日志。
