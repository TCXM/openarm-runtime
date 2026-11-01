"""Operator-started relative leader control using the existing whole-arm hold."""

from contextlib import ExitStack, contextmanager
from concurrent.futures import ThreadPoolExecutor
import copy
import math
from pathlib import Path
import threading
import time
import uuid

from openarm_leader.mapping import delta, SOURCE_IDS
from .arm import ArmHold
from .config import bus_lease
from .control import check_modes
from .report import Report
from .transport import ClassicBus
from .joints import JOINT_LIMITS
from .gravity import prepare_gravity


def leader_sample(reader):
    sample, error, _ = reader.snapshot()
    if error or sample is None or not sample.fresh(time.monotonic()):
        raise RuntimeError(f"主臂反馈中断或过期：{error or '无新鲜读数'}")
    # Operator-approved: voltage-only warnings on fresh, valid feedback are
    # informational, as in the leader viewer. Combined fault bits still stop.
    if any(code & ~1 for code in sample.errors) or any(sample.torque):
        faults = []
        for i, code in enumerate(sample.errors):
            if code & ~1:
                voltage = f"，{sample.voltages[i]:.1f} V" if i < len(sample.voltages) else ""
                kind = "电压告警" if code == 1 else "舵机告警"
                faults.append(f"ID{i + 1} {kind} 0x{code:02x}{voltage}")
        for i, torque in enumerate(sample.torque):
            if torque:
                faults.append(f"ID{i + 1} 已使能（torque_enable={torque}）")
        raise RuntimeError("主臂：" + "；".join(faults))
    return sample


def relative_offsets(mapping, side, origin, sample):
    """Relative takeover with the upstream 65-degree gripper motor span."""
    offsets = {i + 1: sign * delta(sample.positions[mid - 1], origin.positions[mid - 1]) * 360 / 4096
               for i, (mid, sign) in enumerate(zip(SOURCE_IDS, mapping.data["signs"][side]))}
    _, start = mapping.angles(side, origin.positions)
    _, opening = mapping.angles(side, sample.positions)
    if start is None or opening is None:
        raise ValueError("请先完成夹爪标定")
    offsets[8] = (opening - start) * (65 if side == "left" else -65)
    if any(not math.isfinite(v) for v in offsets.values()):
        raise ValueError("Non-finite leader target")
    return offsets


def follower_targets(side, base, offsets):
    targets = {}
    for mid, (lower, upper) in enumerate(JOINT_LIMITS[side], 1):
        goal = math.degrees(base[mid]) + offsets[mid]
        if not math.isfinite(goal):
            raise ValueError("Non-finite follower target")
        targets[mid] = math.radians(max(lower, min(upper, goal)))
    return targets


class Teleoperation:
    def __init__(self, profile, mapping, readers, report_root):
        self.profile, self.mapping, self.readers = profile, mapping, readers
        self.report_root = Path(report_root)
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.worker = None
        self.owner = None
        self.closed = False
        self.message = "待机 · 启动时对齐当前姿态"

    @property
    def busy(self):
        return self.lock.locked()

    def start(self, owner, sides):
        if (not self.profile.mapping_verified or not 1 <= len(sides) <= 2
                or len(set(sides)) != len(sides) or any(s not in ("left", "right") for s in sides)):
            raise ValueError("请确认机械臂映射")
        if self.closed or not self.lock.acquire(blocking=False):
            raise RuntimeError("已有跟随任务")
        try:
            mapping = copy.deepcopy(self.mapping)
            selected = {s: self.readers[mapping.data["ports"][s]] for s in sides}
            for side, reader in selected.items():
                if any(mapping.device(side)[k] is None for k in ("zero", "open", "closed")):
                    raise ValueError("请先完成并保存主臂标定")
                leader_sample(reader)
            self.cancel.clear()
            self.owner = owner
            self.message = "正在检查从臂并接管…"
            self.worker = threading.Thread(target=self._run, args=(mapping, selected), daemon=True)
            self.worker.start()
        except BaseException:
            self.owner = None
            self.lock.release()
            raise

    def stop(self, owner=None):
        if owner is None or owner == self.owner:
            self.cancel.set()

    def close(self):
        self.closed = True
        self.stop()
        if self.worker:
            self.worker.join(timeout=30)
            if self.worker.is_alive():
                raise RuntimeError("Teleoperation shutdown timed out")

    def _run(self, mapping, readers):
        report = None
        try:
            report = Report(self.report_root / f"teleop-{uuid.uuid4().hex}.json",
                            {"action": "relative_teleoperation", "sides": list(readers),
                             "mapping": mapping.data, "target_mode": "direct_mit",
                             "joint_limits_deg": JOINT_LIMITS, "gripper_excursion_deg": 65})
            prepared = threading.Barrier(len(readers), timeout=10)
            ready = threading.Event()
            state_lock, report_lock = threading.Lock(), threading.Lock()
            holding, errors = set(), []
            origins = {}

            def record(side, event):
                with report_lock:
                    report.record(dict(event, side=side))

            def cancelled():
                if self.cancel.is_set():
                    return True
                for reader in readers.values():
                    leader_sample(reader)
                return False

            @contextmanager
            def stop_peers_on_error():
                try:
                    yield
                except BaseException:
                    self.cancel.set()
                    prepared.abort()
                    raise

            def run_arm(side, reader):
                try:
                    with ExitStack() as stack:
                        interface = self.profile.interface(side)
                        stack.enter_context(bus_lease(interface, exclusive=True))
                        bus = ClassicBus(interface, self.profile.feedback_timeout_s)
                        stack.callback(bus.close)
                        preflight = bus.preflight()
                        record(side, {"event": "preflight", "motors": preflight})
                        gravity = prepare_gravity(self.profile, side, bus, preflight)
                        check_modes(bus, True, self.cancel.is_set)
                        # No arm enables until every selected bus passes preflight.
                        prepared.wait()
                        control = stack.enter_context(ArmHold(
                            bus, cancelled, lambda event: record(side, event),
                            lambda states: None, tracking=True, gravity=gravity))
                        # Cancel peer loops before this arm's disable readback waits.
                        stack.enter_context(stop_peers_on_error())
                        period = 1 / self.profile.rate_hz
                        with state_lock:
                            holding.add(side)
                            if len(holding) == len(readers):
                                # Capture both origins together after both ramps.
                                origins.update({s: leader_sample(r) for s, r in readers.items()})
                                ready.set()
                                self.message = ("双臂" if len(readers) == 2 else "单臂") + "跟随中 · 直接位置跟随"
                        previous = time.monotonic()
                        recorded = False
                        while not self.cancel.is_set():
                            now = time.monotonic()
                            if now - previous > max(0.25, 3 * period):
                                raise RuntimeError("Teleoperation control deadline missed")
                            previous = now
                            if ready.is_set():
                                if not recorded:
                                    record(side, {"event": "takeover", "leader_positions": origins[side].positions,
                                                  "follower_base": control.base})
                                    recorded = True
                                offsets = relative_offsets(mapping, side, origins[side], leader_sample(reader))
                                targets = follower_targets(side, control.base, offsets)
                            else:
                                # Keep the first ready arm serviced while its peer ramps.
                                targets = dict(control.base)
                            control.track(targets)
                            self.cancel.wait(max(0, period - (time.monotonic() - now)))
                except BaseException as exc:
                    with state_lock:
                        if not (self.cancel.is_set() and isinstance(exc, (InterruptedError, threading.BrokenBarrierError))):
                            errors.append(f"{side}: {type(exc).__name__}: {exc}")
                    self.cancel.set()
                    prepared.abort()

            with ThreadPoolExecutor(max_workers=len(readers), thread_name_prefix="openarm-follow") as pool:
                futures = [pool.submit(run_arm, side, reader) for side, reader in readers.items()]
                for future in futures:
                    future.result()
            if errors:
                raise RuntimeError("；".join(errors))
            self.message = "已停止 · 所选从臂均已确认失能"
        except BaseException as exc:
            self.message = f"已结束：{type(exc).__name__}: {exc}"
            if report:
                try:
                    report.record({"event": "ended", "error": self.message})
                except Exception:
                    pass
        finally:
            self.owner = None
            self.lock.release()


def attach(server, profile, mapping, readers, report_root, mapping_lock, calibration_busy, saved):
    session = Teleoperation(profile, mapping, readers, report_root)
    with server.gui.add_folder("真实从臂跟随"):
        server.gui.add_markdown("接管当前姿态，直接跟随；到关节边界时裁剪。停止或页面断开会失能。")
        choice = server.gui.add_dropdown("跟随机械臂", options=("双臂", "右臂", "左臂"), initial_value="双臂")
        start = server.gui.add_button("开始跟随")
        stop = server.gui.add_button("停止 · 失能", color="red")
        status = server.gui.add_markdown(session.message)

    @start.on_click
    def begin(event):
        if event.client is None:
            return
        with mapping_lock:
            try:
                if calibration_busy() or not saved():
                    raise ValueError("请先完成采集并保存标定")
                sides = {"双臂": ("left", "right"), "右臂": ("right",), "左臂": ("left",)}[choice.value]
                session.start(event.client.client_id, sides)
            except Exception as exc:
                session.message = str(exc)

    @stop.on_click
    def end(_event):
        session.stop()

    @server.on_client_disconnect
    def disconnect(client):
        session.stop(client.client_id)

    def refresh():
        status.content = session.message
        start.disabled = session.busy or calibration_busy() or not saved()
        choice.disabled = session.busy

    return session, refresh
