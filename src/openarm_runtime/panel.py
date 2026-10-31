"""User-operated whole-arm hold. Startup and slider edits before enable are passive."""

from datetime import datetime, timezone
from pathlib import Path
import math
import threading
import time
import uuid

from .telemetry import IDS
from .config import bus_lease, finite
from .transport import ClassicBus
from .arm import ArmHold, FOLLOWER_KP, FOLLOWER_KD
from .report import Report
from .control import check_modes


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
        self.message = "未使能。点击整臂使能后保持当前位置，再拖动滑条。"

    @property
    def busy(self):
        return self.lock.locked()

    def start(self, owner, side, supported, allow_unreadable):
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
        self.message = f"{interface}：只读预检，然后整臂使能…"
        self.worker = threading.Thread(target=self._run,
                                       args=(interface, allow_unreadable), daemon=True)
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

    def _run(self, interface, allow_unreadable):
        connection = self.connections[interface]
        report = None
        try:
            report = Report(self.report_root / (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex + ".json"),
                {"source": "viser_user", "action": "arm_hold", "interface": interface,
                 "kp": FOLLOWER_KP, "kd": FOLLOWER_KD, "allow_unreadable_mode": allow_unreadable})
            connection.close()
            with bus_lease(interface, exclusive=True):
                bus = ClassicBus(interface, self.profile.feedback_timeout_s)
                try:
                    report.record({"event": "preflight", "motors": bus.preflight()})
                    modes = check_modes(bus, allow_unreadable, self.cancel.is_set)
                    report.record({"event": "modes", "readback": modes})

                    def publish(states):
                        with connection.shared.lock:
                            connection.shared.states = dict(states)
                            connection.shared.error = ""
                            connection.shared.message = "整臂MIT位置保持 · 实时控制反馈"
                            connection.shared.responses += len(states)
                            connection.shared.rx_classic += len(states)

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
    with server.gui.add_folder("整臂控制 · Classic 1 Mbps"):
        server.gui.add_markdown(
            "先使能一条臂，8个电机保持当前角度；拖动滑条调整目标，松手后继续保持。"
            "\n\n滑条为相对本次使能位置的±30°偏移，目标按限速平滑推进。"
            "停止会失能整臂；断开操作浏览器也会停止。不会自动置零。")
        speed = server.gui.add_slider("目标速度 (°/s)", min=0.1, max=5, step=0.1, initial_value=5.0)
        supported = server.gui.add_checkbox("机械臂已就绪，可进入使能保持", initial_value=False)
        unreadable = server.gui.add_checkbox("RID10不可读时按MIT协议试运行", initial_value=False)
        stop = server.gui.add_button("停止并失能整臂", color="red")
        status = server.gui.add_markdown(session.message)
        server.gui.add_markdown(f"参考配置 Kp：`{list(FOLLOWER_KP)}`\n\nKd：`{list(FOLLOWER_KD)}`")
    rows, enable_buttons = [], []
    for side, label in (("right", "右臂"), ("left", "左臂")):
        interface = profile.interface(side)
        with server.gui.add_folder(f"{label} · {interface} · 关节滑条"):
            enable = server.gui.add_button(f"使能{label}并保持当前位置")
            enable_buttons.append(enable)
            @enable.on_click
            def begin(event, side=side):
                if event.client is None:
                    return
                try:
                    session.start(event.client.client_id, side, supported.value, unreadable.value)
                    for row_side, _, _, _, _, slider in rows:
                        if row_side == side:
                            slider.value = 0.0
                except Exception as exc:
                    session.message = str(exc)
            for mid in IDS:
                name = f"J{mid}" if mid < 8 else "夹爪 ID8"
                current = server.gui.add_markdown(f"**{name}** · 等待角度")
                slider = server.gui.add_slider(f"{label} {name} 目标偏移 (°)",
                                               min=-30, max=30, step=0.1, initial_value=0.0, disabled=True)
                rows.append((side, interface, mid, name, current, slider))
                @slider.on_update
                def target(event, side=side, mid=mid, slider=slider):
                    if event.client is None:
                        return
                    try:
                        session.set_target(event.client.client_id, side, mid, slider.value)
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
        for handle in (*enable_buttons, supported, unreadable):
            handle.disabled = session.busy
        now = time.monotonic()
        snapshots = {iface: conn.shared.snapshot()[0] for iface, conn in connections.items()}
        for side, interface, mid, name, current, slider in rows:
            state = snapshots[interface].get(mid)
            if state is None:
                current.content = f"**{name}** · 未收到反馈"
            else:
                age = "" if state.fresh(now) else "（反馈已过期）"
                current.content = f"**{name}** · 当前 {math.degrees(state.position):+.2f}° {age}"
            slider.disabled = not (session.ready and session.side == side)

    return session, refresh
