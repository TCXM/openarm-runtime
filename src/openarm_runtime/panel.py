"""User-operated whole-arm hold. Startup and slider edits before enable are passive."""

from datetime import datetime, timezone
from pathlib import Path
import threading
import time
import uuid

from .telemetry import IDS
from .config import bus_lease, finite
from .transport import ClassicBus
from .arm import ArmHold, FOLLOWER_KP, FOLLOWER_KD
from .report import Report
from .control import check_modes
from .calibration import calibrate_arm


class MotionSession:
    def __init__(self, profile, connections, report_root):
        self.profile, self.connections = profile, connections
        self.report_root = Path(report_root)
        self.lock = threading.Lock()
        self.target_lock = threading.Lock()
        self.cancel = threading.Event()
        self.worker = None
        self.owner = None
        self.side = None
        self.ready = False
        self.closed = False
        self.offsets = dict.fromkeys(IDS, 0.0)
        self.speed = 5.0
        self.message = "待机 · 请选择机械臂和操作"

    @property
    def busy(self):
        return self.lock.locked()

    def start(self, owner, side, supported, allow_unreadable, *, calibration=False):
        if not supported:
            raise ValueError("请先确认机械臂可进入使能保持")
        if not self.profile.mapping_verified:
            raise ValueError("机器配置尚未确认左右映射")
        interface = self.profile.interface(side)
        if self.closed or not self.lock.acquire(blocking=False):
            raise RuntimeError("已有机械臂使能或正在切换")
        self.cancel.clear()
        self.owner, self.side = owner, side
        with self.target_lock:
            self.offsets = dict.fromkeys(IDS, 0.0)
        self.message = f"{'左臂' if side == 'left' else '右臂'} · 正在检查…"
        self.worker = threading.Thread(target=self._run,
                                       args=(interface, allow_unreadable, calibration), daemon=True)
        try:
            self.worker.start()
        except BaseException:
            self.lock.release()
            raise

    def set_target(self, owner, side, mid, offset):
        finite(offset, -30, 30, "offset_deg")
        if not self.ready or owner != self.owner or side != self.side or mid not in IDS:
            raise RuntimeError("请由使能该臂的浏览器操作滑条")
        with self.target_lock:
            self.offsets[mid] = float(offset)

    def stop(self, owner=None):
        if owner is None or owner == self.owner:
            self.cancel.set()

    def close(self):
        self.closed = True
        self.stop()
        if self.worker:
            self.worker.join(timeout=30)
            if self.worker.is_alive():
                raise RuntimeError("Control worker shutdown timed out")

    def _run(self, interface, allow_unreadable, calibration=False):
        connection = self.connections[interface]
        report = None
        try:
            report = Report(self.report_root / (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex + ".json"),
                {"source": "viser_user", "action": "auto_calibrate" if calibration else "arm_hold", "interface": interface,
                 "kp": FOLLOWER_KP, "kd": FOLLOWER_KD, "allow_unreadable_mode": allow_unreadable})
            connection.close()

            def publish(states):
                with connection.shared.lock:
                    connection.shared.states = dict(states)
                    connection.shared.error = ""
                    connection.shared.message = "自动标定 · Classic" if calibration else "整臂MIT位置保持 · 实时控制反馈"
                    connection.shared.responses += len(states)
                    connection.shared.rx_classic += len(states)

            if calibration:
                def progress(message):
                    self.message = f"{interface} 自动标定：{message}"
                calibrate_arm(self.profile, self.side, cancelled=self.cancel.is_set,
                              record=report.record, publish=publish, progress=progress,
                              allow_unreadable_mode=allow_unreadable)
                self.message = "标定完成 · 零位已写入并验证，机械臂已失能"
                return
            with bus_lease(interface, exclusive=True):
                bus = ClassicBus(interface, self.profile.feedback_timeout_s)
                try:
                    report.record({"event": "preflight", "motors": bus.preflight()})
                    modes = check_modes(bus, allow_unreadable, self.cancel.is_set)
                    report.record({"event": "modes", "readback": modes})

                    with ArmHold(bus, self.cancel.is_set, report.record, publish) as control:
                        self.ready = True
                        self.message = f"{interface} 整臂已使能并保持；拖动该臂滑条即可移动。"
                        period = 1 / self.profile.rate_hz
                        previous = time.monotonic()
                        while not self.cancel.is_set():
                            now = time.monotonic()
                            if now - previous > max(0.25, 3 * period):
                                raise RuntimeError("Whole-arm control deadline missed")
                            previous = now
                            with self.target_lock:
                                offsets, speed = dict(self.offsets), self.speed
                            control.tick(offsets, speed, period)
                            self.cancel.wait(max(0, period - (time.monotonic() - now)))
                    self.message = f"{interface} 已停止，8个电机均已确认失能。"
                finally:
                    self.ready = False
                    bus.close()
        except BaseException as exc:
            self.message = f"已结束：{type(exc).__name__}: {exc}"
            if report:
                try:
                    report.record({"event": "ended", "error": self.message})
                except Exception as failure:
                    self.message += f"；记录失败：{failure}"
        finally:
            self.ready = False
            try:
                if not self.closed:
                    connection.restart()
            except Exception as exc:
                self.message += f"；重新读取失败：{exc}"
            self.owner, self.side = None, None
            self.lock.release()


def attach(server, profile, connections, report_root):
    session = MotionSession(profile, connections, report_root)
    stop = server.gui.add_button("停止 · 失能", color="red")
    status = server.gui.add_markdown(session.message)
    health = server.gui.add_markdown("正在连接…")
    side_choice = server.gui.add_dropdown("机械臂", ("右臂", "左臂"))
    mode = server.gui.add_dropdown("操作", ("自动标定", "手动控制"))
    with server.gui.add_folder("标定") as calibration_folder:
        server.gui.add_markdown(
            "1. 将选中臂摆到[参考姿态](https://docs.openarm.dev/setup/openarm-setup/motor-config/#step-2-zero-position-calibration)。\n"
            "2. 清空运动范围，准备在失能时支撑机械臂。\n"
            "3. 勾选就绪，再点开始。\n\n"
            "会自动运动并写入零位。")
        calibration_ready = server.gui.add_checkbox("姿态已摆好，运动范围已清空", initial_value=False)
        calibrate = server.gui.add_button("开始标定", disabled=True)
    with server.gui.add_folder("手动控制", visible=False) as manual_folder:
        server.gui.add_markdown("使能后保持当前位置；滑条调整相对角度。停止或断开操作页面会失能。")
        supported = server.gui.add_checkbox("机械臂已就绪", initial_value=False)
        enable = server.gui.add_button("使能并保持", disabled=True)
        speed = server.gui.add_slider("速度 (°/s)", min=0.1, max=5, step=0.1, initial_value=5.0)
        sliders = {}
        for mid in IDS:
            name = f"J{mid}" if mid < 8 else "夹爪"
            slider = server.gui.add_slider(f"{name} 偏移 (°)", min=-30, max=30, step=0.1,
                                           initial_value=0.0, disabled=True)
            sliders[mid] = slider
            @slider.on_update
            def target(event, mid=mid, slider=slider):
                if event.client is not None:
                    try:
                        session.set_target(event.client.client_id, selected_side(), mid, slider.value)
                    except Exception as exc:
                        session.message = str(exc)

    def selected_side():
        return "left" if side_choice.value == "左臂" else "right"

    @side_choice.on_update
    @mode.on_update
    def selection_changed(_event):
        calibration_ready.value = supported.value = False

    @calibrate.on_click
    def begin_calibration(event):
        if event.client is not None and mode.value == "自动标定":
            try:
                session.start(event.client.client_id, selected_side(), calibration_ready.value,
                              True, calibration=True)
            except Exception as exc:
                session.message = str(exc)

    @enable.on_click
    def begin(event):
        if event.client is not None and mode.value == "手动控制":
            try:
                session.start(event.client.client_id, selected_side(), supported.value, True)
                for slider in sliders.values():
                    slider.value = 0.0
            except Exception as exc:
                session.message = str(exc)

    @speed.on_update
    def speed_changed(event):
        if event.client is not None and (not session.busy or event.client.client_id == session.owner):
            with session.target_lock:
                session.speed = float(speed.value)

    @stop.on_click
    def cancel(_event):
        session.stop()

    @server.on_client_disconnect
    def disconnect(client):
        session.stop(client.client_id)

    def refresh():
        status.content = session.message
        for handle in (side_choice, mode, supported, calibration_ready):
            handle.disabled = session.busy
        calibrate.disabled = session.busy or not calibration_ready.value
        enable.disabled = session.busy or not supported.value
        calibration_folder.visible = mode.value == "自动标定"
        manual_folder.visible = mode.value == "手动控制"
        now = time.monotonic()
        summaries = []
        for side, label in (("right", "右臂"), ("left", "左臂")):
            states, error, *_ = connections[profile.interface(side)].shared.snapshot()
            fresh = len(states) == 8 and all(s.fresh(now) for s in states.values())
            text = "反馈中断" if error or not fresh else (
                "电机故障" if any(s.status not in (0, 1) for s in states.values()) else
                "已使能" if any(s.status == 1 for s in states.values()) else "未使能")
            summaries.append(f"{label} · {text}")
        health.content = " ｜ ".join(summaries)
        for slider in sliders.values():
            slider.disabled = not (session.ready and session.side == selected_side())

    return session, refresh
