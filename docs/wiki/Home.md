# OpenArm Runtime 使用文档

**适用版本：[v0.6.0](https://github.com/TCXM/openarm-runtime/releases/tag/v0.6.0)** · Python 3.12 · Linux 硬件工作站 · Mac / Windows 浏览器客户端

OpenArm Runtime 将电机控制、主臂遥操、触觉和视觉放在同一个工作区。日常使用只需启动 `openarm-runtime platform`，访问 **http://127.0.0.1:8888**；从其他电脑访问时，只转发这一个端口。

这是 `TCXM/openarm-runtime` 社区项目的使用文档，说明本项目的已发布功能。原生 CAN SDK、模型等上游组件的来源见 [第三方说明](https://github.com/TCXM/openarm-runtime/blob/v0.6.0/THIRD_PARTY.md)。

## 从这里开始

| 你想完成什么 | 阅读页面 |
| --- | --- |
| 已装好工作站，启动并查看全部模块 | [快速上手](Quick-Start.md) |
| 在新机器安装、选择依赖、准备配置 | [安装与配置](Installation.md) |
| 查启动、查看、模拟、标定等命令 | [CLI 命令参考](CLI-Reference.md) |
| 把库接入自己的 Python 项目 | [Python API](Python-API.md) |
| 按流程使用电机、遥操、触觉和相机 | [日常操作](Operations.md) |
| 在 Mac / Windows 打开实时网页、查看日志 | [终端与远程访问](Remote-Access.md) |
| 处理 CAN、端口、串口和腕部相机故障 | [故障排查](Troubleshooting.md) |
| 理解单端口、进程和各个库的关系 | [架构与库](Architecture.md) |

## 常用命令速查

以下启动命令在**已配置好的 Linux 工作站**执行。首次使用先完成 [安装与配置](Installation.md)。

```bash
# 使用安装了运行库及所需 extras 的 Python 环境。
source ~/.venvs/openarm/bin/activate

# 日常入口：电机、遥操、触觉、视觉。
openarm-runtime platform --profile ~/.config/openarm-runtime/platform.json
```

在 **Mac Terminal / Windows PowerShell** 建立转发，保留此终端运行：

```sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:8888:127.0.0.1:8888 USER@WORKSTATION
```

将 `USER`、`WORKSTATION` 替换为工作站账号和主机名或 IP，然后在客户端浏览器打开 **http://127.0.0.1:8888**。完整登录、打开浏览器和排错命令见 [远程访问](Remote-Access.md)。

## 使用范围

- 启动平台或打开电机面板不会自动使能电机；启动后的硬件读取由所配置的子服务决定。
- 电机控制与遥操互斥；触觉和视觉可以同时查看。切换方式见 [日常操作](Operations.md)。
- `calibrate` 会运动并写入硬件零位；`hold_arm` 会使能电机。命令与 API 页面分别标出了这些操作。
- 一台 Mac / Windows 电脑作为浏览器客户端时，只需要浏览器和 SSH 客户端；硬件 SDK 与设备配置留在 Linux 工作站。
- 单端口指浏览器访问入口。各模块仍使用工作站内部端口，平台统一代理页面、图像和 WebSocket。

## 版本与维护

安装示例固定在 v0.6.0；开发分支可能增加不同功能。Python API 接入应固定版本。参见 [变更记录](https://github.com/TCXM/openarm-runtime/blob/v0.6.0/CHANGELOG.md)、[验证记录](https://github.com/TCXM/openarm-runtime/blob/v0.6.0/docs/validation.md) 和 [问题反馈](https://github.com/TCXM/openarm-runtime/issues)。

在线阅读：[GitHub Wiki](https://github.com/TCXM/openarm-runtime/wiki)。本目录保留文档源文件，README 顶部提供 Wiki 入口。文档随代码审查和维护；升级版本时同步核对安装标签、命令参数和 API 契约。
