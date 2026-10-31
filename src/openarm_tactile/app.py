"""Compact, mjviser-style Viser workspace for the Paxini finger-pad sensor."""
from collections import deque
from html import escape
from pathlib import Path
import time

from .source import SENSOR, TactileReader


def serve(device=None, *, simulate=False, port=8891, baudrate=921600, device_id=1):
    import numpy as np
    import viser
    from paxini_sdk.sensor_registry import load_points

    if simulate == bool(device):
        raise ValueError("Choose exactly one of simulation or a serial device")
    coords = np.asarray(load_points(SENSOR), dtype=np.float32)
    if coords.shape != (68, 3):
        raise ValueError("SDK coordinates must contain 68 XYZ points")
    center = coords.mean(axis=0)
    neutral = np.tile(np.array([170, 185, 204], dtype=np.uint8), (68, 1))
    device_label = "模拟" if simulate else Path(device).name
    server = viser.ViserServer(host="127.0.0.1", port=port, label="触觉视图 · " + ("模拟" if simulate else device_label.split("_")[-1].split("-")[0][-4:]))
    try:
        if server.get_port() != port:
            raise RuntimeError("Requested viewer port is occupied")
        server.gui.configure_theme(
            control_layout="fixed", control_width="small", dark_mode=False,
            show_share_button=False, brand_color=(39, 119, 98),
        )
        server.scene.set_up_direction("+z")
        server.scene.add_grid(
            "/grid", width=70, height=70, cell_size=2, section_size=10,
            cell_color=(224, 231, 239), section_color=(184, 198, 216),
            position=(0, float(center[1]), 0),
        )
        # A deliberately plain base provides depth cues; it is not a CAD model.
        body = server.scene.add_box(
            "/sensor/base", dimensions=(24, 24, 2), color=(71, 85, 105),
            position=(0, float(center[1]), 1), opacity=0.25,
        )
        server.scene.add_frame("/axes", axes_length=5, axes_radius=0.08, origin_radius=0.15)
        server.scene.add_label("/model", "IP-M2324-Core · 68 taxels", position=(0, 26, 3))
        cloud = server.scene.add_point_cloud(
            "/sensor/taxels", coords, neutral, point_size=1.2, point_shape="circle",
        )
        lines = server.scene.add_line_segments(
            "/sensor/forces", np.stack([coords, coords], axis=1), (245, 158, 11),
            thickness=1.5, thickness_units="screen", visible=False,
        )
        marker = server.scene.add_icosphere(
            "/sensor/selected", radius=0.9, color=(15, 23, 42),
            wireframe=True, position=tuple(coords[0]), subdivisions=1,
        )
        point_label = server.scene.add_label("/selected_label", "#0", position=tuple(coords[0] + [1, 0, 1]))
        mode = "SIMULATION · 模拟数据" if simulate else "硬件采集"
        mode_color = "#a16207" if simulate else "#277762"
        server.gui.add_html(
            '<div style="padding:4px 0 8px"><strong style="font-size:18px">'
            'IP-M2324-Core</strong><div style="color:#64748b;font-size:12px">'
            'PX6AX-GEN3 / 68 测点</div></div>'
            f'<div style="color:{mode_color};font-weight:600">{mode}</div>'
        )
        if not simulate:
            server.gui.add_html(
                f'<div style="font-size:11px;color:#64748b;overflow-wrap:anywhere">{escape("设备 · " + device_label.split("_")[-1].split("-")[0])}</div>'
            )
        status = server.gui.add_html('<div style="color:#64748b">等待采集…</div>')
        tabs = server.gui.add_tab_group()
        with tabs.add_tab("控制", icon=viser.Icon.SETTINGS):
            paused = server.gui.add_checkbox("暂停显示", initial_value=False)
            reconnect = server.gui.add_button("重新连接", icon=viser.Icon.REFRESH, visible=not simulate)
            view = server.gui.add_dropdown("视角", ("立体", "俯视", "侧视"))
            reset_view = server.gui.add_button("重置视角", icon=viser.Icon.CAMERA)
            server.gui.add_markdown(
                "拖动旋转 · 滚轮缩放\n\n暂停仅冻结画面，采集状态仍更新。"
                "灰色测点表示尚无数据或连接异常。"
            )
        with tabs.add_tab("显示", icon=viser.Icon.EYE):
            scale = server.gui.add_slider("Fz 色标上限", min=1, max=255, step=1, initial_value=10)
            server.gui.add_html(
                '<div style="height:7px;border-radius:4px;background:linear-gradient('
                '90deg,rgb(225,239,255),rgb(37,99,235),rgb(245,85,55))"></div>'
                '<div style="display:flex;justify-content:space-between;color:#64748b;'
                'font-size:11px"><span>0 counts</span><span>色标上限</span></div>'
            )
            vectors = server.gui.add_checkbox("显示 XYZ 力向量", initial_value=True)
            gain = server.gui.add_slider("向量长度 / mm每count", min=0, max=0.5, step=0.01, initial_value=0.05)
            show_body = server.gui.add_checkbox("显示座体示意", initial_value=True)
            server.gui.add_markdown("坐标单位 mm。座体仅作方位示意，不是 CAD 外形。")
        with tabs.add_tab("读数", icon=viser.Icon.CHART_LINE):
            selected = server.gui.add_slider("测点编号", min=0, max=67, step=1, initial_value=0)
            details = server.gui.add_markdown("等待测点数据…")
            plot = server.gui.add_uplot(
                tuple(np.zeros(1) for _ in range(4)),
                ({"label": "秒"}, {"label": "Fx", "stroke": "#ef4444"},
                 {"label": "Fy", "stroke": "#16a34a"}, {"label": "Fz", "stroke": "#2563eb"}),
                title="合力 / counts", scales={"x": {"time": False}}, height=180,
            )
            server.gui.add_markdown(
                "力为原始 counts，未应用厂家标定系数。合力和分布由两次串口读取取得；"
                "不会自动清零。曲线保留最近 300 个显示样本。"
            )

        def set_camera(client):
            offsets = {"立体": [35, -42, 40], "俯视": [0, -0.01, 55], "侧视": [40, 0, 10]}
            client.camera.position = tuple(center + offsets[view.value])
            client.camera.look_at = tuple(center)
            client.camera.up_direction = (0, 1, 0) if view.value == "俯视" else (0, 0, 1)

        @server.on_client_connect
        def connected(client):
            set_camera(client)

        @reset_view.on_click
        @view.on_update
        def reset_camera(event):
            if event.client is not None:
                set_camera(event.client)

        reader = TactileReader(device, simulate=simulate, baudrate=baudrate, device_id=device_id)

        @reconnect.on_click
        def restart(_event):
            reader.restart()

        history = deque(maxlen=300)
        displayed = None
        displayed_sequence = -1
        started = time.monotonic()
        with reader:
            while True:
                tick = time.monotonic()
                snapshot = reader.snapshot()
                state = snapshot["state"]
                healthy = state == "live"
                age = snapshot["age_s"]
                age_text = "—" if age is None else f"{age * 1000:.0f} ms"
                state_text = {"waiting": "等待数据", "live": "采集中", "stale": "数据已过期", "error": "连接异常"}.get(state, state)
                color = "#15803d" if healthy else "#b45309"
                error = escape(snapshot["error"] or "")
                status.content = (
                    f'<div style="font-size:12px;line-height:1.7"><strong style="color:{color}">'
                    f'● {state_text}</strong>{" · 显示已暂停" if paused.value else ""}<br>'
                    f'{snapshot["rate_hz"]:.1f} Hz · 帧 {snapshot["sequence"]} · 数据年龄 {age_text}'
                    f'<div style="color:#b45309;overflow-wrap:anywhere">{error}</div></div>'
                )
                if not paused.value and snapshot["sample"] is not None:
                    displayed = snapshot["sample"]
                    if snapshot["sequence"] != displayed_sequence:
                        displayed_sequence = snapshot["sequence"]
                        if healthy:
                            history.append((tick - started, *displayed.resultant))
                            plot.data = tuple(np.asarray(values) for values in zip(*history))
                i = int(selected.value)
                marker.position = tuple(coords[i])
                point_label.position = tuple(coords[i] + [1, 0, 1])
                point_label.text = f"#{i}"
                body.visible = show_body.value
                lines.visible = bool(healthy and displayed is not None and vectors.value)
                if displayed is None or not healthy:
                    cloud.colors = neutral
                else:
                    force = np.asarray(displayed.forces, dtype=np.float32)
                    strength = np.clip(force[:, 2] / scale.value, 0, 1)
                    colors = np.column_stack([
                        np.interp(strength, [0, .5, 1], [225, 37, 245]),
                        np.interp(strength, [0, .5, 1], [239, 99, 85]),
                        np.interp(strength, [0, .5, 1], [255, 235, 55]),
                    ])
                    cloud.colors = colors.astype(np.uint8)
                    lines.points = np.stack([coords, coords + force * gain.value], axis=1)
                if displayed is not None:
                    values = displayed.forces[i]
                    data_caption = "上次有效读数 · 已失效" if not healthy else ("冻结画面" if paused.value else "当前读数")
                    details.content = (
                        f"**{data_caption}**\n\n| counts | Fx | Fy | Fz |\n|---|---:|---:|---:|\n"
                        f"| 合力 | {displayed.resultant[0]:.2f} | {displayed.resultant[1]:.2f} | {displayed.resultant[2]:.2f} |\n"
                        f"| 测点 #{i} | {values[0]:.2f} | {values[1]:.2f} | {values[2]:.2f} |\n\n"
                        f"测点坐标 / mm：{coords[i, 0]:.2f}, {coords[i, 1]:.2f}, {coords[i, 2]:.2f}\n\n"
                        f"峰值 Fz：{max(p[2] for p in displayed.forces):.2f} counts"
                    )
                time.sleep(max(0, 1 / 30 - (time.monotonic() - tick)))
    finally:
        server.stop()
