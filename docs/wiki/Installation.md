# 安装与配置

[返回首页](Home.md) · [快速开始](Quick-Start.md) · [故障排查](Troubleshooting.md)

本文对应 **v0.6.0 / Python 3.12**。连接机器人、相机和传感器的服务运行在 Linux 工作站；Mac 和 Windows 可以只安装 SSH 客户端并用浏览器访问，无需在客户端安装 Python、CAN SDK 或本项目。远程访问见 [Mac / Windows 连接](Remote-Access.md)。

## 1. 安装发布版

在 Linux 工作站上准备 Python 3.12 和 Git，再创建独立环境：

```sh
mkdir -p ~/.venvs
python3.12 -m venv ~/.venvs/openarm
source ~/.venvs/openarm/bin/activate
python -m pip install "openarm-runtime @ git+https://github.com/TCXM/openarm-runtime.git@v0.6.0"
python -c 'import openarm_runtime; print(openarm_runtime.__version__)'
openarm-runtime --help
```

本项目通过 GitHub 标签与 [Release 附件](https://github.com/TCXM/openarm-runtime/releases/tag/v0.6.0)分发。使用标签可固定项目版本；仓库内的 `uv.lock` 还固定了开发环境的依赖版本。导入核心模块或查看帮助不会打开硬件。

### 按需要选择依赖

| 安装项 | 用途 | 额外要求 |
| --- | --- | --- |
| 无 extra | 配置、状态类型、控制与映射 API | 具体硬件操作时才需要对应依赖 |
| `platform` | NiceGUI 总入口、HTTP / WebSocket 代理 | 仍需安装各子模块依赖 |
| `leader` | Feetech STS3215 串口读取 | 串口访问权限 |
| `viewer` | Viser 机器人与触觉三维界面 | 机器人视图需要模型资源 |
| `tactile` | Paxini 68 点串口采集 | 社区 Paxini SDK、串口权限 |
| `video` | RGB / 深度采集和网页 | 深度另需匹配的 OpenNI2 SDK / helper |
| `follower` | OpenArm 从臂 Classic CAN | Linux SocketCAN、原生 OpenArm CAN SDK 1.4.1 |

例如安装总入口、主臂预览、触觉和视觉：

```sh
python -m pip install "openarm-runtime[platform,leader,viewer,tactile,video] @ git+https://github.com/TCXM/openarm-runtime.git@v0.6.0"
```

要使用电机控制和实体跟随，还需完成下一节。仅安装 `[platform]` 不会自动装齐相机、串口和电机 SDK。

## 2. 电机控制所需的原生 SDK

此部分在连接从臂的 **Linux 工作站**执行。Mac / Windows 的浏览器访问不需要这一步；本项目未提供原生 Windows / macOS 从臂控制方案。

先按 [Linux 原生 SDK 安装说明](https://github.com/TCXM/openarm-runtime/blob/v0.6.0/docs/workstation.md#build-the-official-sdk)构建或复用已验证的安装：

- OpenArm CAN SDK：1.4.1，对应提交 `bfd4e52194aefe38389fa6e670be3738136facb9`。
- CLI11：对应提交 `4160d259d961cd393fd8d67590a8c7d210207348`。
- 构建工具：Git、CMake、C++ 编译器、uv。

在已激活的 `~/.venvs/openarm` 环境里，指定实际 SDK 安装前缀后安装 Python 绑定。下面的 `/path/to/openarm-can-1.4.1` 必须替换为真实目录：

```sh
export OPENARM_CAN_PREFIX=/path/to/openarm-can-1.4.1
test -f "$OPENARM_CAN_PREFIX/lib/cmake/OpenArmCAN/OpenArmCANConfig.cmake"
uv pip install nanobind==2.10.2 scikit-build-core==1.1.1
export CMAKE_ARGS="-Dnanobind_DIR=$(python -m nanobind --cmake_dir) -DCMAKE_DISABLE_FIND_PACKAGE_OpenArmCAN=OFF -DCMAKE_PREFIX_PATH=$OPENARM_CAN_PREFIX -DCMAKE_INSTALL_RPATH=$OPENARM_CAN_PREFIX/lib"
export CMAKE_BUILD_PARALLEL_LEVEL=4
uv pip install --no-build-isolation-package openarm-can "openarm-runtime[follower] @ git+https://github.com/TCXM/openarm-runtime.git@v0.6.0"
python -c 'import openarm_runtime, openarm_can; print(openarm_runtime.__version__, openarm_can.__version__)'
uv pip check
```

依赖项目的 uv 构建设置不会自动传入你的应用环境，因此应保留上面的 CMake 参数与 `--no-build-isolation-package`。移动原生 SDK 目录后需要重新构建绑定；仅修改 Python 路径不能修复动态库引用。

## 3. 创建机器配置

配置、标定、日志和模型放在仓库外。首次配置时下载或克隆对应标签的示例；下面用 `/path/to/openarm-runtime` 表示 v0.6.0 源码目录。

```sh
git clone --branch v0.6.0 --depth 1 https://github.com/TCXM/openarm-runtime.git /path/to/openarm-runtime
cd /path/to/openarm-runtime
mkdir -p ~/.config/openarm ~/.config/openarm-runtime
# 已存在的文件保持不变；更新配置前另行备份并人工合并。
test -e ~/.config/openarm/classic.json || cp classic.example.json ~/.config/openarm/classic.json
test -e ~/.config/openarm/video-runtime.json || cp video.example.json ~/.config/openarm/video-runtime.json
test -e ~/.config/openarm-runtime/platform.json || cp platform.example.json ~/.config/openarm-runtime/platform.json
```

按实际硬件编辑以下文件：

| 文件 | 必须核对的内容 |
| --- | --- |
| `~/.config/openarm/classic.json` | 左右臂与 CAN 接口的对应关系；物理核对后才将 `mapping_verified` 设为 `true` |
| `~/.config/openarm/video-runtime.json` | RGB 设备路径、左右腕映射、深度 helper 路径、分辨率 / 帧率 |
| `~/.config/openarm-runtime/platform.json` | 各子服务命令、设备路径、端口、模型路径和报告目录 |
| `~/.config/openarm/leader.json` | 由主臂标定流程保存的软件零位与夹爪端点；不从示例捏造数据 |

[Classic 示例](https://github.com/TCXM/openarm-runtime/blob/v0.6.0/classic.example.json)中的 `left=can1`、`right=can0` 是示例映射，不是所有机器的固定接线。程序要求 Classic CAN 1 Mbps、FD 关闭、`restart-ms=0` 和健康链路；运行时只检查，不自动改 CAN。检查与恢复见 [故障排查](Troubleshooting.md)。

[平台示例](https://github.com/TCXM/openarm-runtime/blob/v0.6.0/platform.example.json)中的 `command` 是参数数组：

- `{python}` 使用启动平台的 Python 解释器，因此该环境必须包含子服务依赖。
- `~/` 会展开为当前用户目录；`cwd` 和 `log_dir` 的相对路径以平台配置文件目录为基准。
- 不执行 shell 展开，不能把管道、`&&`、`$HOME` 等拼成一个命令字符串。
- 平台入口默认 `8888`；内部端口必须互不重复，并与对应命令的 `--port` 一致。
- 只配置单个触觉视图时设 `extra_ports: []`；双视图默认占用主触觉端口加 10、加 11。

触觉命令中的 `--right-device` 表示页面第二个传感器。当前双传感器布局中，两块都安装在左臂，不能据参数名推断成左右机械臂各一块。

深度 helper 不随 pip 自动安装。Astra Pro 应使用同一套匹配的 OpenNI2 头文件、运行库和 Orbbec 驱动，构建方式见 [深度相机配置](https://github.com/TCXM/openarm-runtime/blob/v0.6.0/docs/video.md#astra-pro-depth-helper)。

## 4. 准备模型并启动

在安装了 `viewer` 的环境中下载、校验固定版本的官方模型：

```sh
source ~/.venvs/openarm/bin/activate
openarm-runtime prepare --asset-root ~/.cache/openarm-models
openarm-runtime platform --profile ~/.config/openarm-runtime/platform.json
```

打开工作站上的 `http://127.0.0.1:8888`，或从 Mac / Windows 建立一个 SSH 转发后访问。平台启动不会使能电机或自动跟随。首次迁移应先正常停止原来独立启动的控制与采集服务，使平台能够管理其生命周期。

完整日常流程见 [操作指南](Operations.md)；只需复制常用启动命令时看 [快速开始](Quick-Start.md)。

## 5. 只用软件数据体验平台

这个演示在独立端口运行视频和单个触觉模拟页，不配置电机或遥操启动命令，也不需要 CAN SDK、相机或串口设备。仅安装 core 不够；模拟界面仍需要 `platform,tactile,viewer,video` 的 Python 依赖：

```sh
source ~/.venvs/openarm/bin/activate
python -m pip install "openarm-runtime[platform,tactile,viewer,video] @ git+https://github.com/TCXM/openarm-runtime.git@v0.6.0"
ss -ltn
```

先确认 `18888`、`18893`、`18890`、`18891`、`15004` 未被其他服务使用。若有占用，选择另一组空闲端口，并同步修改服务端口及命令中的 `--port`；不要复用真实控制器的端口。

在 Linux 工作站生成外部配置。代码只在文件不存在时创建，不会覆盖已有配置：

```sh
python - <<'PYCODE'
import json
from pathlib import Path

path = Path("~/.config/openarm-runtime/platform-sim.json").expanduser()
config = {
    "port": 18888,
    "log_dir": "~/.local/state/openarm-platform-sim",
    "services": {
        "control": {"port": 18893},
        "teleop": {"port": 18890},
        "tactile": {
            "port": 18891,
            "extra_ports": [],
            "command": ["{python}", "-m", "openarm_runtime", "tactile",
                        "--simulate", "--port", "18891"],
        },
        "vision": {
            "port": 15004,
            "command": ["{python}", "-m", "openarm_runtime", "video",
                        "--simulate", "--port", "15004"],
        },
    },
}
path.parent.mkdir(parents=True, exist_ok=True)
try:
    with path.open("x") as output:
        json.dump(config, output, ensure_ascii=False, indent=2)
        output.write("\n")
except FileExistsError:
    print(f"保留已有文件，请先核对其内容：{path}")
else:
    print(f"已创建：{path}")
PYCODE
```

确认配置仍然只有上述模拟命令后启动：

```sh
openarm-runtime platform --profile ~/.config/openarm-runtime/platform-sim.json
```

在工作站浏览器打开 `http://127.0.0.1:18888`，进入“触觉”或“视觉”，页面会明确标记模拟数据。“电机控制”和“遥操”没有启动命令，显示未连接是预期行为；此演示不包含运动控制模拟。终端按 `Ctrl+C` 退出演示及其子服务。

从 Mac / Windows 访问时，把 [远程访问](Remote-Access.md) 中的转发端口替换为 `18888`，然后打开本地 `http://127.0.0.1:18888`。

## 6. 把库接入自己的项目

在自己的 Python 3.12 项目中，用固定版本作为依赖：

```sh
uv add "openarm-runtime[leader,viewer] @ git+https://github.com/TCXM/openarm-runtime.git@v0.6.0"
```

开发应用和本库时，可把本库放在任意目录，用 editable 安装：

```sh
python -m pip install -e "/path/to/openarm-runtime[leader,viewer]"
# uv 管理的应用可改用：
uv add --editable "/path/to/openarm-runtime[leader,viewer]"
```

Python 源码修改通常在重启应用后生效；依赖、入口点或构建配置变化时重新安装。交付应用前改回已发布标签或固定提交，并更新应用的锁文件。API 用法见 [Python API](Python-API.md)。

## 7. 在本仓库中开发

开发模式使用仓库自己的 `.venv`，与上文的发布版环境分开：

```sh
cd /path/to/openarm-runtime
uv sync --locked --extra platform --extra leader --extra viewer --extra tactile --extra video
uv run --no-sync python -m openarm_runtime --help
```

需要完整硬件依赖时，先准备原生 SDK，再用仓库的安装脚本：

```sh
export OPENARM_CAN_PREFIX=/path/to/openarm-can-1.4.1
sh sync.sh
python3 run_platform.py
```

`sync.sh` 安装锁定的全部 extra 并配置原生绑定构建参数；它不设置 CAN，也不构造电机对象。`run_platform.py` 使用仓库 `.venv` 和外部平台配置。开发测试只能使用离线回归、模拟服务或经操作者明确安排的实机验证。
