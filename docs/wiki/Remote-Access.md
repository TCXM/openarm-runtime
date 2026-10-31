# 终端与远程访问：Linux / Mac / Windows

[文档首页](Home.md) · 适用 v0.6.0

**Linux 工作站运行硬件服务；Mac / Windows 运行 SSH 客户端和浏览器。** 统一网页入口为 `http://127.0.0.1:8888`。以下 `USER@WORKSTATION` 是占位符，替换为工作站账号和主机名或 IP。

## 命令在哪里执行

| 位置 | 执行内容 |
| --- | --- |
| Linux 工作站终端，或 SSH 登录后的远端提示符 | 启动平台、查 CAN／串口、查看服务日志 |
| Mac 的 Terminal | `ssh` 建立连接／转发；`open` 打开本机浏览器 |
| Windows 的 PowerShell / Windows Terminal | `ssh` 建立连接／转发；`Start-Process` 打开本机浏览器 |
| 客户端浏览器 | 访问本机的 `127.0.0.1:8888`，数据通过 SSH 到达工作站 |

只做远程查看时，客户端不需要安装 Python、CAN SDK 或本项目。硬件工作站应已配置 SSH 登录和 OpenArm Runtime，参见 [安装与配置](Installation.md)。

## 情况 A：平台已经在工作站运行

### Mac Terminal

打开 Terminal，在终端 A 执行并保留：

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:8888:127.0.0.1:8888 USER@WORKSTATION
```

在另一个本机终端 B 执行，或手动输入浏览器地址：

```bash
open http://127.0.0.1:8888
```

### Windows PowerShell

先确认 SSH 客户端可用：

```powershell
ssh -V
```

如果提示找不到 `ssh`，在 Windows 的“可选功能”安装 **OpenSSH 客户端**。也可以在管理员 PowerShell 执行下列安装命令，完成后重新打开普通 PowerShell；此步骤只在缺少客户端时需要。参见 [Microsoft OpenSSH 安装文档](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse)。

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
```

在普通 PowerShell 窗口 A 执行并保留：

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:8888:127.0.0.1:8888 USER@WORKSTATION
```

在另一个本机 PowerShell 窗口 B 打开浏览器：

```powershell
Start-Process "http://127.0.0.1:8888"
```

这里使用单行命令，不需要 Bash 的续行反斜杠。首次登录请核对工作站的 SSH 主机指纹，然后按提示输入凭据；密码输入时不回显。

## 情况 B：同时登录工作站并启动平台

Mac Terminal 和 Windows PowerShell 都可以使用下面的命令。这里省略 `-N`，同一个 SSH 会话提供远端 shell 和端口转发：

```sh
ssh -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:8888:127.0.0.1:8888 USER@WORKSTATION
```

登录成功后，以下两行是在 **Linux 工作站的远端提示符**下执行：

```bash
source ~/.venvs/openarm/bin/activate
openarm-runtime platform --profile ~/.config/openarm-runtime/platform.json
```

如果实际使用的是开发仓库环境，改为：

```bash
cd /path/to/openarm-runtime
python3 run_platform.py
```

两种启动方式选择一种；已有平台时不要再启动。随后在 Mac / Windows 的本机浏览器打开 **http://127.0.0.1:8888**。平台留在前台运行，输出直接显示在这个 SSH 终端。

## 参数与端口说明

| 参数 | 作用 |
| --- | --- |
| `-N` | 只建立转发，不启动远端命令；终端持续等待是正常状态 |
| `-L 127.0.0.1:8888:127.0.0.1:8888` | 客户端回环地址的 8888 转到工作站回环地址的 8888 |
| `-o ExitOnForwardFailure=yes` | 转发监听建立失败时退出，例如本地端口已被占用 |
| `-o ServerAliveInterval=30` | 无服务端数据时，每 30 秒发送保活消息 |
| `-p 2222` | 若管理员配置了非默认 SSH 端口，可添加此选项；数值按实际修改 |

`ExitOnForwardFailure` 不能证明远端平台已经启动；远端目标拒绝连接时仍须检查服务。参数语义见 [OpenSSH ssh 手册](https://man.openbsd.org/ssh) 和 [ssh_config 手册](https://man.openbsd.org/ssh_config)。

本地 8888 被其他程序占用时，只改本地监听端口：

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8889:127.0.0.1:8888 USER@WORKSTATION
```

此时在客户端打开 **http://127.0.0.1:8889**，工作站平台仍运行在 8888。默认内部端口 8893、8890、8891、8901、8902、5003 都由网关转发，无需额外 SSH 转发。平台要求通过 localhost 地址访问，不能直接用 `http://工作站IP:8888` 替代隧道。

## 从客户端查看实时终端日志

平台已经运行时，另开一个本机 Terminal / PowerShell 窗口：

```sh
# 这里引号里的命令由远端 Linux 执行，两种客户端都适用。
ssh USER@WORKSTATION "tail -n 80 -F ~/.local/state/openarm-platform/vision.log"
```

其他模块把 `vision.log` 替换为 `control.log`、`teleop.log` 或 `tactile.log`。这些是平台启动的子服务日志；配置了 `log_dir` 时使用该目录。平台自身的日志默认在其启动终端，`platform.log` 只有自行重定向输出时才存在。

按 Ctrl+C 结束日志跟踪。需要完整交互终端时，先 `ssh USER@WORKSTATION`，再执行 Linux 命令。

## 浏览器打不开时逐层检查

**先在 Linux 工作站检查平台：**

```bash
ss -ltnp 'sport = :8888'
curl -fsS http://127.0.0.1:8888/api/status | python3 -m json.tool
```

**再在 Mac 本机检查转发：**

```bash
lsof -nP -iTCP:8888 -sTCP:LISTEN
curl -fsS http://127.0.0.1:8888/api/status
```

**或者在 Windows PowerShell 检查转发：**

```powershell
Get-NetTCPConnection -LocalPort 8888 -State Listen
Test-NetConnection 127.0.0.1 -Port 8888
Invoke-RestMethod -Uri "http://127.0.0.1:8888/api/status" | ConvertTo-Json -Depth 6
```

诊断接口 `/api/status` 属于平台内部实现；以上只做读取，不会切换模块。工作站本地请求失败时查平台启动输出；本地成功但客户端失败时查 SSH 隧道；网页可打开但单个模块离线时查该模块日志。

## 结束与保留会话

- `ssh -N` 窗口按 Ctrl+C：仅结束转发，远端平台仍可能运行。
- 日志跟踪窗口按 Ctrl+C：仅结束 `tail`。
- 平台前台窗口按 Ctrl+C：请求平台关闭自己启动的服务，并等待清理结果。
- 关闭浏览器：不代表电机已经失能，也不代表远端服务停止。

需要长期保留终端时，可使用工作站已安装的终端复用工具并按其运维方式管理；不要依赖直接关闭 SSH 窗口完成正常停机。完整停止顺序见 [日常操作](Operations.md)。
