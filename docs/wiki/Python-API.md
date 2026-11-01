# Python API 与库参考

适用版本：**v0.6.0 · Python 3.12**。项目安装名是 `openarm-runtime`，Python 导入名按模块区分。所有模块的导入均不会打开硬件；构造同步设备对象、启动采集器或进入控制上下文才会产生相应操作。

[返回首页](Home.md) · [安装与依赖](Installation.md) · [命令参考](CLI-Reference.md)

## 模块与依赖

| 导入模块 | 主要接口 | 使用时需要的 extra |
| --- | --- | --- |
| `openarm_runtime` | `Profile`、`ArmConnection`、`MotorState`、`MotorRange`、`hold_arm`、`calibrate_arm` | 只读 Linux SocketCAN 遥测用 core；实际控制用 `follower` 和原生 SDK |
| `openarm_leader` | `LeaderBus`、`LeaderReader`、`Sample`、`Mapping` | 串口采集用 `leader`；软件映射本身无 SDK 依赖 |
| `openarm_tactile` | `TactileReader`、`Sample`、`SENSOR`、`POINT_COUNT` | 真实采集和软件仿真均用 `tactile`；显示页面再加 `viewer` |
| `openarm_video` | `CameraConfig`、`Frame`、`CameraReader`、`VideoCapture`、编解码和网页接口 | `video`；真实深度另需 OpenNI2 helper |
| `openarm_platform` | `PlatformConfig`、`load_config` | 配置读取用 core；启动统一网页用 `platform` |
| `openarm_viewer` | 机器人和触觉可视化实现 | `viewer`；优先通过 CLI 使用 |

例如，采集视频并供自己的程序处理，只需安装 `[video]`，不需要启动平台或 Viser。需要机器人网页的使用方式见[操作指南](Operations.md)。

## 通用生命周期与数据约定

- 后台采集类使用 `with` 或显式 `start()` / `close()`；`ArmConnection` 使用 `restart()` / `close()`。
- `snapshot()` 返回最新已知数据，不代表刚刚完成采样。检查错误状态和数据年龄，再用于业务逻辑。
- 时间戳采用 `time.monotonic()`，单位秒，用于本机年龄计算，不是 Unix 时间或跨设备同步时间。
- 读取器报错时可能保留旧数据；禁止把旧数据当成当前反馈。关闭失败会抛出异常，应交由上层处理。
- 物理控制要求同一 CAN 总线只有一个控制者。进入控制或标定前先停止该总线的只读采集器。

## 主臂：`openarm_leader`

### 同步读取

`LeaderBus(path)` 在构造时打开串口，以 1 Mbps 读取 STS3215 的 ID 1–8。只发读取请求，不写舵机寄存器。

以下示例会读取真实串口；先替换设备占位符，并确认设备没有被其他读取器占用：

```python
from openarm_leader import LeaderBus

with LeaderBus("/dev/serial/by-id/YOUR_LEADER") as bus:
    sample = bus.sample()
    print("编码器计数:", sample.positions)
    print("电压 V:", sample.voltages)
    print("使能状态:", sample.torque)
    print("故障位:", sample.errors)
```

`Sample` 中的各元组按 ID 1–8 排列。`positions` 是原始编码器计数；`torque` 是使能状态，**不是力矩值**。`received_at` 为一次采样的起始时间，`duration` 为读取耗时。`fresh(now)` 要求年龄小于 0.4 秒。

### 后台读取

```python
import time
from openarm_leader import LeaderReader

with LeaderReader("/dev/serial/by-id/YOUR_LEADER") as reader:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        sample, error, count = reader.snapshot()
        if not error and sample is not None and sample.fresh(time.monotonic()):
            print(count, sample.positions)
        time.sleep(0.1)
```

`LeaderReader` 在读取失败后自动尝试重新连接。仅电压故障位不阻止有效数据读取；其他故障、损坏数据和超时会作为错误处理。物理跟随还会检查主臂是否处于失能状态。

### 软件标定与映射

`Mapping(left, right, path)` 接收两条不同串口路径以及外部 JSON 标定文件路径，标定按设备身份保存。

| 方法 | 含义 |
| --- | --- |
| `capture(side, sample, kind, now)` | 记录 `zero`、`open` 或 `closed`；`side` 为 `left` / `right`，拒绝过期或已使能主臂样本 |
| `angles(side, positions)` | 返回 `(angles, opening)`：七关节角度为弧度，夹爪开度在 `[0, 1]`；夹爪未标定则为 `None` |
| `save()` | 将软件标定写入指定 JSON 文件 |
| `swap()` | 交换左右设备映射，之后需显式保存 |

尚未采集关节零位时，`angles()` 会用编码器 2048 作为显示基准；得到角度不等于完成标定。实际跟随需完整校验标定。该软件标定不会写入舵机或从臂电机的硬件零位。

## 从臂只读反馈：`ArmConnection`

此接口运行于 Linux SocketCAN，发送参数/反馈读取请求，不使能电机、不写控制目标。CAN 应已配置为经过验证的 Classic 1 Mbps 链路。

```python
import time
from openarm_runtime import ArmConnection

reader = ArmConnection("can0", "classic")
try:
    reader.restart()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        states, error, message, replies, missed = reader.shared.snapshot()
        if not error:
            now = time.monotonic()
            positions = {
                mid: state.position
                for mid, state in states.items()
                if state.fresh(now, 0.4)
            }
            print("关节位置 rad:", positions)
        else:
            print("读取错误:", error)
        time.sleep(0.2)
finally:
    reader.close()
```

`MotorState` 包含 `motor_id`、`position`（rad）、`velocity`（rad/s）、`torque`（Nm）、`mos_temperature`、`rotor_temperature`、`status`、`received_at` 和 `frame_format`。初始或掉线时 `states` 可能不完整；整臂逻辑必须验证所需全部 ID 的状态。

`MotorRange(position, velocity, torque)` 是固件数据解码范围，不能作为机械碰撞或运动边界。

## 从臂控制与硬件标定

以下接口会控制真实电机。先完成[操作指南](Operations.md)中的设备映射、链路和工作空间检查。API 使用外部 `Profile.load(path)`，配置示例见 [`classic.example.json`](https://github.com/TCXM/openarm-runtime/blob/v0.6.0/classic.example.json)。

### 保持当前姿态：`hold_arm`

```python
import signal
import threading
import time
from openarm_runtime import Profile, hold_arm

profile = Profile.load("/path/to/classic.json")
stop = threading.Event()
for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda *_: stop.set())

period = 1.0 / profile.rate_hz
offsets = dict.fromkeys(range(1, 9), 0.0)
try:
    # 进入上下文会使能所选物理臂；零偏移表示保持刚采集的姿态。
    with hold_arm(profile, "right", cancelled=stop.is_set) as arm:
        while not stop.is_set():
            started = time.monotonic()
            arm.tick(offsets, speed=1.0, period=period)
            stop.wait(max(0.0, period - (time.monotonic() - started)))
except InterruptedError:
    pass  # 上下文已执行退出清理；其他控制/失能异常仍向外传播。
```

`hold_arm(profile, side, *, allow_unreadable_mode=False, cancelled=..., record=..., publish=...)` 管理独占总线、预检、使能、退出失能和连接关闭。调用者负责循环调度，没有隐藏控制线程。

| `arm.tick` 参数 | 单位与限制 |
| --- | --- |
| `offsets` | 必须提供 ID 1–8；相对本次使能姿态的偏移角，单位度，范围 ±30° |
| `speed` | 0.1–5°/s |
| `period` | 0.02–0.1 s；默认配置为 20 Hz |

这是 Python 尽力调度的控制循环，不提供硬实时保证。`record(event)` 和 `publish(states)` 回调应保持快速；回调异常也会触发退出清理。无法确认失能时会抛出错误，不应在上层静默忽略。

`allow_unreadable_mode=True` 仅针对已验证的 RID10 读取超时情况；如果读回明确的非 MIT 模式，仍会拒绝控制。默认保持 `False`。

### 自动标定：`calibrate_arm`

```python
# 接口调用形式；仅在操作者决定执行物理标定时调用。
# zeros = calibrate_arm(
#     profile, "left", cancelled=stop.is_set,
#     record=report.record, publish=on_feedback, progress=on_progress,
# )
```

完整签名为 `calibrate_arm(profile, side, *, cancelled=..., record=..., publish=..., progress=..., allow_unreadable_mode=False)`。

该同步调用会使能、运动、寻找止挡、到达计算零位、失能，并写入/验证硬件零位。返回 `{motor_id: raw_zero_radians}`，覆盖 ID 1–8，数值使用写入前的编码器坐标。需提供取消回调和持久日志，例如 `openarm_runtime.report.Report(...).record`；CLI 和网页已实现相应调度。

标定失败可能发生在部分硬件零位已写入之后，不会自动回滚。开始前阅读[标定说明](https://github.com/TCXM/openarm-runtime/blob/v0.6.0/docs/calibration.md)，不要把此接口当作只读估计函数。

## 触觉：`openarm_tactile`

`TactileReader(device=None, *, simulate=False, baudrate=921600, device_id=1)` 在 `start()` 或进入 `with` 后开始采集。必须选择串口设备或 `simulate=True` 中的一种。`device_id` 是板上模块 ID，与 `/dev/ttyUSB*` 的数字无关；项目验证的设备使用 ID 2。

下面的示例生成软件数据，不访问真实串口。仿真仍需要 `[tactile]` 中 SDK 提供的传感器点位表：

```python
import time
from openarm_tactile import TactileReader, POINT_COUNT

with TactileReader(simulate=True) as reader:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = reader.snapshot()
        if state["state"] == "error":
            raise RuntimeError(state["error"])
        if state["state"] == "live":
            sample = state["sample"]
            assert len(sample.forces) == POINT_COUNT == 68
            print("合力原始计数:", sample.resultant)
            print("第一个测点 XYZ:", sample.forces[0])
            break
        time.sleep(0.02)
    else:
        raise TimeoutError("未收到触觉仿真数据")
```

真实采集将构造参数换为 `TactileReader("/dev/serial/by-id/YOUR_TACTILE", device_id=2)`。

`snapshot()` 是字典，包含 `state`、`error`、`sample`、`sequence`、`age_s`、`rate_hz`。仅 `state == "live"` 表示可使用当前样本；其他状态为 `waiting`、`stale`、`error`、`closed`。超过 0.5 秒的样本视为过期。

`Sample.resultant` 为 XYZ 合力，`Sample.forces` 为 68 个 XYZ 测点；数据是原始计数，**不是标定后的牛顿值**。Fx/Fy 有符号，Fz 无符号。合力与分布来自独立事务，不是严格同步的一帧。采集速率上限为 60 Hz。

失败后不会自动转入仿真或自动重连；在已启动的 reader 上调用 `restart()` 显式重连。`close()` 最多等待工作线程 3 秒并报告失败；关闭后创建新实例。

## 视觉：`openarm_video`

### 采集配置与数据

`CameraConfig(name, device="", kind="rgb", width=640, height=480, fps=30, preview_width=None, preview_height=None, depth_helper=None, uri=None)` 只保存配置。

- RGB 指定 `device`。深度指定 `kind="depth"`、外部 `depth_helper`，必要时指定 OpenNI `uri`。
- `preview_width` 和 `preview_height` 必须同时设置，只影响网页预览；Python 获取原始采集分辨率。
- `load_cameras(path)` 从外部 `{"cameras": [...]}` JSON 文件读取配置元组。
- RGB `Frame.image` 为 H×W×3、`uint8`、BGR；深度为 H×W、`uint16`、毫米，0 表示无效。
- `Frame` 还包含 `received_at`、`sequence`、`pixel_format`。图像由采集器持有，消费者修改前应 `.copy()`。

### 多路软件采集

此示例只需 `[video]`，无需摄像头或深度 SDK：

```python
import time
from openarm_video import CameraConfig, VideoCapture, encode_frame

configs = [CameraConfig("head"), CameraConfig("depth", kind="depth")]
with VideoCapture(configs, simulate=True) as capture:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        frame, error, count = capture.snapshot("head")
        if error:
            raise RuntimeError(error)
        if frame is not None and frame.fresh(time.monotonic()):
            data, mime = encode_frame(frame)
            print(frame.image.shape, count, len(data), mime)
            break
        time.sleep(0.02)
    else:
        raise TimeoutError("未收到视频仿真帧")
```

`VideoCapture(configs, *, source_factory=None, stale_after=3, simulate=False)` 持有一组具名读取器。构造不访问硬件；`with` / `start()` 才开始。`snapshot(name)` 返回 `(frame_or_none, error, count)`；`status()` 返回按相机名组织的状态。关闭时尝试清理所有读取器，即使某个关闭失败。

单路可用 `CameraReader(config, *, source_factory=None, stale_after=3, retry_interval=1, simulate=False)`；读取失败后会重新连接。关闭后的实例不能重新启动。可注入 `source_factory(config)`，返回提供 `read() -> Frame` 与有界、可重复 `close()` 的自定义源；采集器负责关闭它。

### 同步源、编码与网页

| 接口 | 用途 |
| --- | --- |
| `OpenCVCamera(config)` | 构造时打开同步 RGB 源；`read()` 返回 `Frame`，支持 `with` |
| `OpenNIDepthCamera(config)` | 启动已配置的深度 helper；`read()` 返回深度 `Frame` |
| `build_depth_helper(include_dir, library_dir, output)` | 使用外部匹配的 OpenNI2 SDK 编译 helper，不打开设备 |
| `encode_frame(frame, *, raw=False, near=0.3, far=4)` | 返回 `(bytes, MIME)`；RGB 为 JPEG，深度为 PNG；深度预览范围参数单位米 |
| `create_app(capture)` | 返回 Flask app，采集器生命周期由调用者管理 |
| `serve(capture, host="127.0.0.1", port=5003)` | 先绑定端口，再启动采集并阻塞服务；退出时关闭采集器 |

`raw=True` 的深度编码保留原始毫米数值。相关 HTTP 帧路径和图像语义见[视觉文档](https://github.com/TCXM/openarm-runtime/blob/v0.6.0/docs/video.md)。

## 平台配置与接口边界

```python
from openarm_platform import load_config

config = load_config("/path/to/platform.json")
print(config.port)
for page in config.pages:
    print(page.id, page.port)
```

`load_config(path=None)` 和 `PlatformConfig` 可在 core 环境中使用，不启动子服务。配置参数和启动命令见[架构说明](Architecture.md)及[命令参考](CLI-Reference.md)。

平台的 `openarm_platform.app.create_app`、服务管理类和 `/api/*` 路由服务于内部网页，并非承诺兼容的公共硬件 REST API；v0.6.0 也未开放 OpenAPI 页面。集成采集/控制优先使用本文 Python API，启动平台使用 `openarm-runtime platform`。

0.x 系列消费者应固定版本标签或提交。完整契约以 [v0.6.0 API 文档](https://github.com/TCXM/openarm-runtime/blob/v0.6.0/docs/api.md)及对应实现为准；不兼容 API 变更会写入变更记录并提升次版本号。
