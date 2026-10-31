"""Physical leader → virtual follower. Calibration writes only a local JSON file."""

import math
import copy
import threading
import time

from openarm_viewer.model import load_model
from openarm_viewer.scene import check_port, create_scene
from .mapping import Mapping, SOURCE_IDS
from .transport import Reader


def serve(asset_root, left, right, calibration, port=8890):
    import numpy as np
    import viser

    mapping = Mapping(left, right, calibration)
    check_port(port)
    models = {side: load_model(asset_root, side, component=True)
              for side in ("body", "left", "right")}
    readers = {path: Reader(path) for path in (left, right)}
    lock = threading.RLock()
    server = viser.ViserServer(host="127.0.0.1", port=port, label="OpenArm 遥操作")
    if server.get_port() != port:
        server.stop()
        raise RuntimeError("Requested viewer port is occupied")
    try:
        roots, visuals = create_scene(server, models)
        for side, label in (("left", "左臂"), ("right", "右臂")):
            server.scene.add_label(f"/label_{side}", text=label,
                                   position=(0, 0.32 if side == "left" else -0.32, 0.88))
        statuses = {side: server.gui.add_markdown(f"{label} · 连接中")
                    for side, label in (("left", "左臂"), ("right", "右臂"))}
        align = server.gui.add_button("重新对齐", hint="把当前主臂姿态作为虚拟双臂的起点")
        swap = server.gui.add_button("交换左右")
        message = server.gui.add_markdown("")

        @align.on_click
        def align_both(_event):
            with lock:
                previous = copy.deepcopy(mapping.data)
                try:
                    now = time.monotonic()
                    for side in ("left", "right"):
                        sample, error, _ = readers[mapping.data["ports"][side]].snapshot()
                        if error:
                            raise ValueError("主臂未连接")
                        mapping.capture(side, sample, "zero", now)
                    message.content = "已对齐，移动主臂即可。"
                except Exception as exc:
                    mapping.data = previous
                    message.content = f"对齐失败：{exc}"

        @swap.on_click
        def swap_sides(_event):
            with lock:
                mapping.swap()
                message.content = "已交换左右。"

        settings = server.gui.add_folder("设置", expand_by_default=False)
        with settings:
            save = server.gui.add_button("保存设置")

        @save.on_click
        def save_mapping(_event):
            with lock:
                try:
                    mapping.save()
                    message.content = "已保存。"
                except Exception as exc:
                    message.content = f"保存失败：{exc}"

        panels = {}
        for side, label in (("left", "左臂"), ("right", "右臂")):
            with settings, server.gui.add_folder(f"{label}设置", expand_by_default=False):
                for kind, text in (("open", "记录夹爪张开"), ("closed", "记录夹爪闭合")):
                    button = server.gui.add_button(f"{label} · {text}")

                    @button.on_click
                    def capture(_event, side=side, kind=kind):
                        with lock:
                            try:
                                sample, error, _ = readers[mapping.data["ports"][side]].snapshot()
                                if error:
                                    raise ValueError(error)
                                mapping.capture(side, sample, kind, time.monotonic())
                                message.content = "夹爪位置已记录。"
                            except Exception as exc:
                                message.content = f"采集失败：{exc}"

                with server.gui.add_folder(f"{label} · 方向校对", expand_by_default=False):
                    for index, mid in enumerate(SOURCE_IDS):
                        reverse = server.gui.add_checkbox(
                            f"J{index + 1} 反向",
                            initial_value=mapping.data["signs"][side][index] < 0,
                        )

                        @reverse.on_update
                        def change_sign(_event, side=side, index=index, handle=reverse):
                            with lock:
                                mapping.data["signs"][side][index] = -1 if handle.value else 1

                with server.gui.add_folder(f"{label} · 原始读数", expand_by_default=False):
                    status = server.gui.add_markdown("")
                    table = server.gui.add_markdown("")
                panels[side] = status, table
        for reader in readers.values():
            reader.start()
        print(f"Leader virtual teleoperation http://127.0.0.1:{port}", flush=True)
        last_ui = 0
        while True:
            now = time.monotonic()
            with lock, server.atomic():
                for side in ("left", "right"):
                    path = mapping.data["ports"][side]
                    sample, error, count = readers[path].snapshot()
                    ready = sample is not None and sample.fresh(now) and not error
                    roots[side].visible = bool(ready)
                    angles, opening = ([0] * 7, None)
                    exceeded = []
                    if ready:
                        angles, opening = mapping.angles(side, sample.positions)
                    if ready:
                        values = {f"openarm_{side}_joint{i + 1}": angle
                                  for i, angle in enumerate(angles)}
                        finger = f"openarm_{side}_finger_joint1"
                        limit = models[side].joint_map[finger].limit
                        # Official left opens positive, right negative; zero is closed.
                        extent = limit.upper if side == "left" else limit.lower
                        values[finger] = (opening or 0) * extent
                        for name, angle in values.items():
                            bounds = models[side].joint_map[name].limit
                            if not bounds.lower - 1e-3 <= angle <= bounds.upper + 1e-3:
                                exceeded.append(name.removeprefix(f"openarm_{side}_"))
                        visuals[side].update_cfg(np.asarray(
                            [values[name] for name in visuals[side].get_actuated_joint_names()]))
                    if now - last_ui >= 0.25:
                        device = mapping.device(side)
                        status, table = panels[side]
                        state = "已连接" if ready else "连接中"
                        if ready and any(e & 1 for e in sample.errors):
                            state = "已连接 · 电压偏低"
                        if error:
                            state = ("串口无权限" if "Permission denied" in error else
                                     "供电不稳" if "voltage" in error else "连接中")
                        label = "左臂" if side == "left" else "右臂"
                        statuses[side].content = f"{label} · {state}"
                        status.content = (
                            f"**{state}**\n\n`{path}`\n\n"
                            f"读数帧：{count} · 姿态：{'已对齐' if device['zero'] is not None else '未对齐'}"
                            f" · 夹爪：{'已标定' if opening is not None else '等待两个端点'}\n\n"
                            + (f"供电 {min(sample.voltages):.1f}–{max(sample.voltages):.1f} V\n\n"
                               if ready and sample.voltages else "")
                            + (f"{error}\n\n" if error else "")
                            + (f"舵机告警：{sample.errors}\n\n" if sample and any(sample.errors) else "")
                            + (f"超出模型关节范围（未裁剪）：{', '.join(exceeded)}\n\n" if exceeded else "")
                            + (f"注意：主臂使能状态 {sample.torque}\n\n" if sample and any(sample.torque) else "")
                        )
                        if sample:
                            rows = ["| 主臂ID | 原始位置 | 虚拟角度° |", "|---|---:|---:|"]
                            for index, mid in enumerate(SOURCE_IDS):
                                angle_text = f"{math.degrees(angles[index]):.1f}" if ready else "—"
                                rows.append(f"| {mid} → J{index + 1} | {sample.positions[mid - 1]} | {angle_text} |")
                            grip = f"{100 * opening:.0f}%" if opening is not None else "未标定"
                            rows.append(f"| 8 夹爪 | {sample.positions[7]} | {grip} |")
                            table.content = "\n".join(rows) + (
                                f"\n\n采样耗时 {sample.duration * 1000:.1f} ms"
                                + (" · 原始值为历史数据，已过期" if not ready else ""))
                if now - last_ui >= 0.25:
                    last_ui = now
            time.sleep(1 / 30)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            for reader in readers.values():
                try:
                    reader.close()
                except Exception as exc:
                    print(f"Leader reader shutdown: {exc}", flush=True)
        finally:
            server.stop()
