"""Shared bimanual display, with optional user-operated whole-arm control panel."""

from __future__ import annotations

import math
import time
from pathlib import Path

from .model import REVISION, joint_configuration, load_model
from .scene import check_port, create_scene
from openarm_runtime.telemetry import IDS, STATUS, ArmConnection, model_ready


def validate_interfaces(left: str, right: str):
    if left == right or {left, right} != {"can0", "can1"}:
        raise ValueError("The two arms must use distinct can0 / can1 interfaces")


def serve(asset_root: Path, left: str, right: str, port: int, frame_format: str = "auto",
          *, control_profile=None, report_root=None):
    import numpy as np
    import viser

    validate_interfaces(left, right)
    check_port(port)
    models = {
        side: load_model(asset_root, side, component=True) for side in ("body", "left", "right")
    }
    title = "OpenArm 双臂 · 手动控制" if control_profile else "OpenArm 双臂 · READ ONLY"
    server = viser.ViserServer(host="127.0.0.1", port=port, label=title)
    if server.get_port() != port:
        server.stop()
        raise RuntimeError("Requested viewer port is occupied")
    connections = {iface: ArmConnection(iface, frame_format) for iface in (left, right)}
    control_session = None
    refresh_controls = lambda: None
    try:
        roots, visuals = create_scene(server, models)
        labels = {
            side: server.scene.add_label(
                f"/status_{side}",
                text=f"{side} · 等待数据",
                position=(0.0, 0.32 if side == "left" else -0.32, 0.88),
            )
            for side in ("left", "right")
        }
        if control_profile:
            from openarm_runtime.panel import attach
            control_session, refresh_controls = attach(server, control_profile, connections, report_root)
        with server.gui.add_folder("完整双臂 · 状态显示"):
            server.gui.add_markdown(
                (f"控制映射：左臂 `{left}`，右臂 `{right}`。模型显示不代表已校准零位。"
                 if control_profile else
                "**仅读取，不使能、不失能、不改参数、不回零、不控制运动。**\n\n"
                f"启动映射：左臂模型 ← `{left}`，右臂模型 ← `{right}`。"
                "默认按官方 can1 左 / can0 右，实体对应仍需核实。\n\n"
                "每路独立检查8个电机的 can_br；主机配置不匹配会停止该路。"
                "compat兼容模式允许4/9混合，以Classic请求接收两种回复，不代表全FD验收。")
            )
            mapping = server.gui.add_dropdown(
                "显示映射（不改变接线）", ("启动映射", "交换左右显示")
            )
            if control_profile:
                mapping.disabled = True
            gripper = server.gui.add_dropdown(
                "夹爪几何（仅显示）", ("固定，仅数值", "官方 pinch 参考")
            )
            server.gui.add_markdown(
                "夹爪默认仅数值；可选官方 pinch 1:1参考映射，**不是实测开口或已校准传动**。"
                "编码器零位与方向未标定，不钳位掩盖超限读数。"
            )
        panels = {}
        for iface, connection in connections.items():
            with server.gui.add_folder(f"{iface} · 独立链路"):
                status = server.gui.add_markdown("等待只读配置检查…")
                metadata = server.gui.add_markdown("")
                reconnect = server.gui.add_button(f"重新连接 {iface}（只读）")
                table = server.gui.add_markdown("")
                panels[iface] = (status, metadata, table)

                @reconnect.on_click
                def reconnect_reader(_event, conn=connection, button=reconnect):
                    if control_session and control_session.busy:
                        return
                    button.disabled = True
                    try:
                        conn.restart()
                    except Exception as exc:
                        with conn.shared.lock:
                            conn.shared.error = str(exc)
                    finally:
                        button.disabled = False

        with server.gui.add_folder("电机详情 · 16个电机"):
            selected = server.gui.add_dropdown(
                "电机", tuple(f"{iface}/ID{i}" for iface in (left, right) for i in IDS)
            )
            fields = {
                name: server.gui.add_text(name, initial_value="—", disabled=True)
                for name in (
                    "角度 (rad)",
                    "速度 (rad/s)",
                    "电机估计力矩 (Nm)",
                    "MOS / 转子 (℃)",
                    "数据年龄 / 状态 / 帧类型",
                )
            }
        server.gui.add_markdown(
            f"官方 OpenArm v2 · `{REVISION[:12]}`\n\n"
            "状态轮询只读；程序不自动修改通信参数。\n\n"
            "某臂任一J1–J7过期0.4秒即隐藏该臂，另一臂继续。参考夹爪还要求ID8新鲜。"
        )
        for connection in connections.values():
            connection.restart()
        print(f"{title} http://127.0.0.1:{port} · {left} / {right}", flush=True)
        while True:
            refresh_controls()
            now = time.monotonic()
            snapshots = {iface: conn.shared.snapshot() for iface, conn in connections.items()}
            display = {"left": left, "right": right}
            if mapping.value == "交换左右显示":
                display = {"left": right, "right": left}
            gripper_mode = "official-pinch" if gripper.value == "官方 pinch 参考" else "fixed"
            with server.atomic():
                for side, iface in display.items():
                    states, error, *_ = snapshots[iface]
                    complete = model_ready(states, now, error, gripper_mode != "fixed")
                    roots[side].visible = complete
                    if complete:
                        cfg = joint_configuration(
                            visuals[side].get_actuated_joint_names(), states, side, gripper_mode
                        )
                        visuals[side].update_cfg(np.asarray(cfg))
                    labels[side].text = f"{side} ← {iface} · " + (
                        "实时（映射未校准）" if complete else "缺失 / 过期，臂已隐藏"
                    )
                for iface, (states, error, message, responses, missed) in snapshots.items():
                    fresh = {i for i, state in states.items() if state.fresh(now)}
                    enabled = [i for i in fresh if states[i].status == 1]
                    faults = [i for i in fresh if states[i].status not in (0, 1)]
                    heading = "实时状态" if len(fresh) == 8 and not error else "配置检查 / 数据缺失"
                    if enabled:
                        heading = f"警告：{enabled} 已使能，请勿手动摆臂"
                    if faults:
                        heading = f"电机故障：{faults}"
                    status, metadata, table = panels[iface]
                    status.content = (
                        f"### {heading}\n新鲜：{len(fresh)}/8 · 回复：{responses} · "
                        f"缺失：{missed}\n\n{error or message}"
                    )
                    meta = connections[iface].shared.metadata()
                    info = meta["link"].get("linkinfo", {}).get("info_data", {})
                    rate = info.get("bittiming", {}).get("bitrate", "—")
                    data_rate = info.get("data_bittiming", {}).get("bitrate", "—")
                    metadata.content = (
                        f"请求模式：`{meta['requested_format']}` · TX帧：`{meta['format']}` · RX Classic/FD："
                        f"{meta['rx_classic']}/{meta['rx_fd']}\n\n"
                        f"主机仲裁/数据速率：{rate}/{data_rate}\n\n"
                        f"电机 can_br（4=Classic 1M，9=FD 1M/5M）：`{meta['baud_codes']}`"
                    )
                    rows = ["| ID | 角度 (°) | 状态 |", "|---|---:|---|"]
                    for i in IDS:
                        state = states.get(i)
                        if state is None:
                            rows.append(f"| {i} | — | 从未收到 |")
                        else:
                            text = STATUS.get(state.status, f"UNKNOWN {state.status:#x}")
                            if i not in fresh:
                                text = f"已过期 ({now - state.received_at:.1f}s)"
                            rows.append(f"| {i} | {math.degrees(state.position):.2f} | {text} |")
                    table.content = "\n".join(rows)
                iface, motor = selected.value.split("/ID")
                state = snapshots[iface][0].get(int(motor))
                values = (
                    ("—",) * 5
                    if state is None
                    else (
                        f"{state.position:.5f}",
                        f"{state.velocity:.4f}",
                        f"{state.torque:.4f}",
                        f"{state.mos_temperature} / {state.rotor_temperature}",
                        f"{now - state.received_at:.3f}s · {STATUS.get(state.status, 'UNKNOWN')} · "
                        f"{state.frame_format}" + (" · 已过期" if not state.fresh(now) else ""),
                    )
                )
                for field, value in zip(fields.values(), values):
                    field.value = value
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        if control_session:
            control_session.close()
        for connection in connections.values():
            try:
                connection.close()
            except Exception as exc:
                print(f"Reader shutdown: {exc}", flush=True)
        server.stop()
