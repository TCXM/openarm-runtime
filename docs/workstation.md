# Linux workstation installation

Python3.12、uv、Git、CMake和C++编译工具。主臂/Viser安装只需README中的extras，
本页的C++流程仅用于真实从臂。SDK和模型放仓库外；不自动配置CAN或运行电机。

## 官方SDK构建

已有工作站可以复用已验证安装前缀；新机器按以下固定版本构建：

```sh
export DEPS="$HOME/robot_deps"
mkdir -p "$DEPS"
git clone https://github.com/CLIUtils/CLI11.git "$DEPS/CLI11"
git -C "$DEPS/CLI11" checkout 4160d259d961cd393fd8d67590a8c7d210207348
cmake -S "$DEPS/CLI11" -B "$DEPS/CLI11/build" \
  -DCLI11_BUILD_TESTS=OFF -DCLI11_BUILD_EXAMPLES=OFF \
  -DCMAKE_INSTALL_PREFIX="$DEPS/cli11-v2.5.0"
cmake --build "$DEPS/CLI11/build" --parallel 4
cmake --install "$DEPS/CLI11/build"

git clone https://github.com/enactic/openarm_can.git "$DEPS/openarm_can"
git -C "$DEPS/openarm_can" checkout bfd4e52194aefe38389fa6e670be3738136facb9
export OPENARM_CAN_PREFIX="$DEPS/openarm-can-1.4.1"
cmake -S "$DEPS/openarm_can" -B "$DEPS/openarm_can/build" \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF \
  -DCMAKE_PREFIX_PATH="$DEPS/cli11-v2.5.0" \
  -DCMAKE_INSTALL_PREFIX="$OPENARM_CAN_PREFIX" \
  -DCMAKE_INSTALL_RPATH="$OPENARM_CAN_PREFIX/lib"
cmake --build "$DEPS/openarm_can/build" --parallel 4
cmake --install "$DEPS/openarm_can/build"
```

在本仓库根目录执行 `sh sync.sh` 安装全部extras。脚本先安装构建依赖，然后显式
传入nanobind路径、C++前缀和RPATH，再构建Python绑定。`uv.lock`固定依赖。
不要移动C++安装前缀；更换它后重新构建绑定。

## 在其他项目安装从臂extra

以下命令在调用方已激活的Python3.12虚拟环境执行：

```sh
uv pip install nanobind==2.10.2 scikit-build-core==1.1.1
export CMAKE_ARGS="-Dnanobind_DIR=$(python -m nanobind --cmake_dir) -DCMAKE_DISABLE_FIND_PACKAGE_OpenArmCAN=OFF -DCMAKE_PREFIX_PATH=$OPENARM_CAN_PREFIX -DCMAKE_INSTALL_RPATH=$OPENARM_CAN_PREFIX/lib"
export CMAKE_BUILD_PARALLEL_LEVEL=4
uv pip install --no-build-isolation-package openarm-can "${OPENARM_RUNTIME_ROOT}[follower]"
```

应用于wheel时，将最后参数换为 `'/path/to/openarm_runtime-0.1.0-py3-none-any.whl[follower]'`。
普通pip/uv不会继承依赖仓库的uv构建选项，因此调用方也需要上述构建配置。

离线验证：

```sh
python -c 'import openarm_runtime, openarm_can; print(openarm_runtime.__version__, openarm_can.__version__)'
uv pip check
```

## 配置与运行

- 从 `classic.example.json` 开始，外部配置指定左右CAN接口。物理对应确认后设置
  `mapping_verified=true`；新机器不能沿用其他机器的左右判断。
- 当前验证配置：Classic1 Mbps、FD关闭、restart-ms0，电机can_br4；工具检查而不重配。
- 主臂使用两条稳定串口路径，外部JSON保存软件参考位置和夹爪端点。
- 串口权限由操作系统提供。临时ACL拔插后可能丢失；长期使用按适配器唯一序列号
  编写限定设备的udev规则，指定实际用户，避免给所有串口放开权限。
- 远程Viser使用SSH转发，服务只监听loopback：
  `ssh -N -L 127.0.0.1:8890:127.0.0.1:8890 workstation`。
- 后台服务应将stdout/stderr重定向到外部日志，避免SSH关闭后残留进程输出阻塞。
  停止服务使用SIGTERM；命令行Ctrl+C也会执行对应清理。

迁移期间原研究仓库仍可能运行。不要同时启动两个真实从臂控制器或两个同串口主臂
reader；保留了与原版本共用的CAN锁目录，但它不能约束第三方程序。
