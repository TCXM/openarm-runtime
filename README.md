# OpenArm Runtime

可独立安装的 OpenArm 底层：Classic CAN 从臂控制、Feetech 主臂读取与映射，
以及可选的 Viser 界面。其他项目通过 Python API 复用，不需要复制代码或启动网页。

当前版本 `0.1.0`，Python 3.12。真实从臂运行要求 Linux SocketCAN。
本地独立 Git 仓库，尚未发布到 GitHub / PyPI。

`bimanual_tactile` 通过 `code/hardware` 集成环境依赖本包；两个仓库采用同级目录。
该环境的 `runtime-revision` 固定所需提交，`sync.sh` 核对版本后安装非editable包。
底层修改和测试统一在本仓库进行，更新研究项目时显式更新它的版本锁。

## 安装

在其他项目的虚拟环境中按需安装，`OPENARM_RUNTIME_ROOT` 指向本仓库：

```sh
# 核心类型、映射、离线工具；不安装GUI和硬件SDK
uv pip install "$OPENARM_RUNTIME_ROOT"

# 主臂读取 + Viser
uv pip install "${OPENARM_RUNTIME_ROOT}[leader,viewer]"

# 联合开发时可使用editable安装
uv pip install -e "${OPENARM_RUNTIME_ROOT}[leader,viewer]"
```

主臂依赖 `feetech-servo-sdk==1.0.0` 和 `pyserial==3.5`；可视化依赖
`viser[urdf]==1.1.1`、`pycollada==0.9.3`。核心包导入不加载它们、不打开硬件。

真实从臂使用 `follower` extra，它包含固定 revision 的官方 OpenArm CAN1.4.1。
该SDK需要预装同版本C++库和特定构建配置，按 [SDK安装](docs/workstation.md) 操作。
在本仓库开发环境安装全部功能：

```sh
export OPENARM_CAN_PREFIX="/path/to/openarm-can-1.4.1"
sh sync.sh
```

## Python API

### 主臂读取

同步读取8个舵机；串口使用 `/dev/serial/by-id/` 稳定路径：

```python
from openarm_leader import LeaderBus

with LeaderBus(port) as leader:
    sample = leader.sample()
    print(sample.positions)   # ID1..8，原始编码器位置
    print(sample.voltages)    # V
    print(sample.errors)      # 保留舵机告警位
```

需要持续读取时使用 `LeaderReader(port)`：`start()` / `snapshot()` / `close()`，
也支持 `with LeaderReader(port) as reader:`。`snapshot()` 返回
`(sample, error, count)`；读取前检查 `sample.fresh(time.monotonic())` 和 `error`。
出现串口断开时自动重连；停止时关闭串口，不写舵机使能、零位或其他参数。

映射使用 `Mapping(left_port, right_port, calibration_path)`：
`angles(side, sample.positions)` 返回7个虚拟关节角（rad）和夹爪开度0–1。
未标定夹爪返回 `None`，不能当作测得的闭合状态。
`capture(side, sample, kind, now)` 的 `kind` 为 `zero` / `open` / `closed`，
`save()` 保存软件标定；偏移与夹爪端点绑定设备身份。

### 从臂只读

```python
from openarm_runtime import ArmConnection

reader = ArmConnection("can0", "classic")
try:
    reader.restart()
    # 在自己的循环中调用：states, error, message, replies, missed
    snapshot = reader.shared.snapshot()
finally:
    reader.close()
```

`MotorState` 中位置为rad、速度rad/s、力矩Nm，含设备状态及单调时钟时间戳。
`MotorRange` 表示实读固件编码范围，不是机械碰撞限制。

### 从臂保持与控制

```python
from openarm_runtime import Profile, hold_arm

profile = Profile.load(machine_config_path)
# 进入上下文会使能选中实物臂并保持当前位置。
with hold_arm(profile, "right", cancelled=stop_event.is_set,
              record=write_event, publish=on_feedback) as arm:
    # 在你的20Hz循环中调用；以下是单个控制周期，不是自动循环。
    arm.tick(offsets_deg, speed=5.0, period=0.05)
```

`offsets_deg` 必须包含ID1–8，值为相对本次使能姿态的±30°目标偏移。
当前支持0.1–5°/s。调用方负责按配置频率调度、及时退出以及传入取消信号。
首次使能前执行只读预检、量程与MIT模式检查；退出上下文尝试全部失能并验证。
RID10不可读的已知硬件需显式 `allow_unreadable_mode=True`；明确非MIT仍拒绝。
总线互斥、增益、零前馈、限速和异常清理复用既有实现。

底层 `ClassicBus` / `ArmHold` 仍可直接导入，常规项目优先使用 `hold_arm()`。
导入任何模块不会使能；`hold_arm` 的上下文进入才是明确的控制操作。

## 命令行

安装后提供唯一命令 `openarm-runtime`，也支持 `python -m openarm_runtime`。

```sh
openarm-runtime --help
openarm-runtime prepare --asset-root "$MODEL_ROOT"
openarm-runtime --config "$MACHINE_CONFIG" check
openarm-runtime --config "$MACHINE_CONFIG" view --asset-root "$MODEL_ROOT"
openarm-runtime --config "$MACHINE_CONFIG" panel --asset-root "$MODEL_ROOT" \
  --report-root "$REPORT_ROOT"
openarm-runtime leader --left-port "$LEADER_LEFT_PORT" --right-port "$LEADER_RIGHT_PORT" \
  --calibration "$CALIBRATION_PATH" --asset-root "$MODEL_ROOT"
```

Viser默认 `127.0.0.1:8890`。`panel` 为真实从臂用户控制，`leader` 为实体主臂驱动
虚拟模型。主臂界面默认实时跟随，主画面仅保留状态、重新对齐和交换左右。
设置中记录夹爪开闭位置、调整关节方向并保存。

机器配置、标定、模型、日志和SDK安装目录由调用方传入，放在仓库外。
保留 `classic.example.json` 作为可移植模板，初始 `mapping_verified=false`。

## 开发与验证

```sh
uv sync --locked
uv run --locked python -m unittest discover -s tests
uv build
```

基础环境可执行离线回归；官方模型测试需安装 `viewer` extra 并设置
`OPENARM_VIEWER_ASSET_ROOT`。测试不修改 `sys.path`，同一套测试可用于已安装wheel。
新版本优先使用wheel/固定Git revision集成，避免研究项目不知不觉跟随底层变化。

目录：
- `src/openarm_runtime/`：CAN协议、状态类型、控制生命周期、配置及CLI。
- `src/openarm_leader/`：串口读取、软件标定、主臂虚拟映射。
- `src/openarm_viewer/`：可选模型与场景；底层不反向依赖此目录。
- `tests/`：协议、退出清理、模式检查、映射、UI与安装回归。

来源与迁移范围见 [ORIGIN.md](ORIGIN.md)，硬件验证边界见
[docs/validation.md](docs/validation.md)。当前已跑通用户操作的从臂保持和主臂到
虚拟模型显示，尚未验收完整主从实机遥操作、绝对零位或长期稳定性。
