# 快速上手

[文档首页](Home.md) · 适用 v0.6.0

本页针对**已完成安装和设备配置**的 Linux 工作站。新机器先看 [安装与配置](Installation.md)，只有 Mac / Windows 客户端则直接看 [终端与远程访问](Remote-Access.md)。

## 1. 在工作站启动统一平台

在 Linux 工作站本机终端或登录工作站后的 SSH 终端执行：

```bash
source ~/.venvs/openarm/bin/activate
openarm-runtime platform --profile ~/.config/openarm-runtime/platform.json
```

如果使用开发仓库及其 `.venv`，可以用仓库启动器替代上面两行：

```bash
cd /path/to/openarm-runtime
python3 run_platform.py
```

两种启动方式选择一种。已有平台运行时直接访问页面，不再启动第二个实例。终端显示 `OpenArm Platform: http://127.0.0.1:8888` 后，保留终端运行。

平台配置指定各模块的启动命令、设备配置和内部端口。`platform` 不会自动发现设备、生成主臂标定或设置 CAN。未填写模块命令时，平台只能连接该端口已经存在的服务。

## 2. 打开实时网页

工作站本机直接打开 **http://127.0.0.1:8888**。

其他电脑在 **Mac Terminal 或 Windows PowerShell** 执行：

```sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:8888:127.0.0.1:8888 USER@WORKSTATION
```

替换账号和主机地址，登录后保持该终端打开。在这台客户端电脑的浏览器访问 **http://127.0.0.1:8888**。`ssh -N` 没有命令提示符、持续等待是正常现象。

这一条转发覆盖所有模块页面、实时图像、3D 视图和触觉嵌套视图。端口被占用、需要更换 SSH 端口等情况见 [远程访问](Remote-Access.md)。

## 3. 使用侧栏打开模块

| 模块 | 查看内容 | 操作提示 |
| --- | --- | --- |
| 电机控制 | 双臂反馈、用户控制、从臂标定 | 初始为只读；使能、移动与标定需在面板主动操作 |
| 遥操 | 主臂反馈、标定、姿态预览、可选物理跟随 | `--follow` 提供跟随控件，启动命令本身不会自动开始跟随 |
| 触觉 | 两个传感器的分布、合力、曲线 | 当前验证布局的两个传感器都在左臂；第二个串口参数名沿用 `--right-device` |
| 视觉 | 头部 RGB、深度、左右腕部图像 | 图像是否实时取决于设备在线状态和帧新鲜度 |

切换到触觉、视觉时，已打开的模块视图会保留。电机控制与遥操属于互斥模式，切换时按界面确认并等待原服务完成关闭。平台复用的外部电机服务需要先由原启动者正常停止，才能交给平台切换管理。

具体控制和标定流程见 [日常操作](Operations.md)。

## 4. 查看日志与状态

另开一个 Linux 工作站终端：

```bash
# 平台启动的子服务输出；可只选需要的日志。
tail -n 80 -F ~/.local/state/openarm-platform/control.log
tail -n 80 -F ~/.local/state/openarm-platform/vision.log
```

平台主进程的输出默认显示在启动终端。子服务日志目录可通过平台配置的 `log_dir` 更改。`tail -F` 用于跟踪日志，按 Ctrl+C 只结束日志查看。

以下查询不会启动或切换模块：

```bash
curl -fsS http://127.0.0.1:8888/api/status | python3 -m json.tool
```

`/api/status` 是 v0.6.0 平台内部诊断接口；应用集成使用 [Python API](Python-API.md)，不把这个地址视为稳定的设备控制协议。

## 5. 结束使用

1. 如果进行了物理控制，先在所属模块停止操作，并确认关闭／失能结果。
2. 在平台启动终端按 Ctrl+C，等待平台关闭自己启动的子服务。
3. 在客户端 SSH 转发终端按 Ctrl+C，结束本地转发。

关闭浏览器或 SSH 转发不会关闭远端平台；平台也不会关闭它复用的外部服务。

## 无设备时先试界面

安装相应 extras 后，在 Linux 开发环境选择以下软件模拟命令；它们各自是独立模块示例：

```bash
# 需要 video extra，不打开真实相机。
openarm-runtime video --simulate --port 5004

# 在另一个终端执行；需要 tactile、viewer extras，不打开真实串口。
openarm-runtime tactile --simulate --port 8895
```

访问各命令打印的本机 URL。平台模拟配置示例见 [安装与配置](Installation.md)；软件模拟不能验证真实 CAN、电机、接线或跟踪精度。

下一步：[常用 CLI 命令](CLI-Reference.md) · [Python API](Python-API.md) · [故障排查](Troubleshooting.md)
