"""Bounded policy traces and simulated previews; no physical robot backend."""
from collections import deque
import copy
from dataclasses import asdict, is_dataclass
import json
import math
from pathlib import Path
import threading

from openarm_policy.adapters import HoldPolicy
from openarm_policy.contracts import ActionChunk


class PreviewSinePolicy(HoldPolicy):
    """Small deterministic demo, used only with SimulatedRobot by this page."""
    def reset(self, context):
        super().reset(context)
        self.base = self.origin = None

    def infer(self, observation):
        if self.base is None:
            self.base = super().infer(observation).steps[0]
            self.origin = observation.captured_at
        steps = tuple(tuple(q + math.radians(3) * math.sin(2 * math.pi *
                            (observation.captured_at - self.origin + i * .05) / 4)
                            if name.endswith('.joint1') else q
                            for name, q in zip(self.context.action.names, self.base))
                      for i in range(20))
        for values in steps:
            self.context.action.validate(values)
        return ActionChunk(observation.sequence, observation.captured_at, .05,
                           steps, self.context.action.names)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('记录包含无效数值')
    return float(value)


class PolicyTrace:
    """Retain the latest 2,000 sampling events, without images or tactile arrays."""
    def __init__(self, source='等待策略'):
        self.lock = threading.Lock()
        self.points = deque(maxlen=2000)
        self.names = []
        self.origin = None
        self.source = source
        self.state = 'idle'
        self.error = ''
        self.commands = self.chunks = self.revision = 0
        self.latency_ms = None
        self.task = ''

    def record(self, event):
        with self.lock:
            kind = event.get('event')
            if kind not in ('context', 'observation', 'command', 'chunk', 'ended'):
                return
            timestamp = number(event.get('time', self.origin or 0))
            if self.origin is None:
                self.origin = timestamp
            self.revision += 1
            if kind == 'context':
                context = event['context']
                if is_dataclass(context):
                    context = asdict(context)
                names = context['action']['names']
                if (context.get('version', 1) != 1 or context['action'].get('version', 1) != 1
                        or context['action'].get('kind', 'joint_position_rad') != 'joint_position_rad'
                        or not names or len(names) > 16 or len(set(names)) != len(names)):
                    raise ValueError('记录的动作协议或关节顺序无效')
                for name in names:
                    side, joint = name.split('.')
                    if side not in ('left', 'right') or joint not in (*[f'joint{i}' for i in range(1, 8)], 'gripper_motor'):
                        raise ValueError('记录包含未知关节')
                self.names = list(names)
                self.task = str(context.get('task', ''))
                self.state = 'starting'
            elif kind == 'observation':
                obs = event['observation']
                streams = obs.streams if hasattr(obs, 'streams') else obs['streams']
                captured = obs.captured_at if hasattr(obs, 'captured_at') else obs['captured_at']
                values = []
                for name in self.names:
                    side, joint = name.split('.')
                    history = streams.get(f'robot/{side}', ())
                    sample = history[-1] if history else None
                    if sample is None:
                        values.append(None)
                        continue
                    valid = sample.valid if hasattr(sample, 'valid') else sample.get('valid', True)
                    data = sample.data if hasattr(sample, 'data') else sample['data']
                    index = 7 if joint == 'gripper_motor' else int(joint[5:]) - 1
                    values.append(number(data[index][0]) if valid else None)
                self.points.append(dict(time=number(captured)-self.origin, kind='feedback', values=values))
                self.state = 'running'
            elif kind == 'command':
                values = list(map(number, event['values']))
                if len(values) != len(self.names):
                    raise ValueError('记录的目标数量与关节顺序不符')
                self.commands += 1
                self.points.append(dict(time=timestamp-self.origin, kind='target', values=values))
            elif kind == 'chunk':
                self.chunks += 1
                self.latency_ms = number(event.get('latency_s', 0)) * 1000
            elif kind == 'ended':
                self.state = str(event.get('state', 'stopped'))
                self.error = '; '.join(filter(None, [str(event.get('error', '')), *map(str, event.get('cleanup_errors', ())) ]))

    def snapshot(self):
        with self.lock:
            return dict(source=self.source, state=self.state, error=self.error, names=list(self.names),
                        points=copy.deepcopy(list(self.points)), commands=self.commands, chunks=self.chunks,
                        latency_ms=self.latency_ms, task=self.task, revision=self.revision)


class PolicyPreview:
    """Shared simulation lifetime; opening a page only reads this state."""
    def __init__(self, record_root):
        self.record_root = Path(record_root).expanduser().resolve()
        self.lock = threading.Lock()
        self.trace = PolicyTrace()
        self.session = self.worker = None
        self.loading = self.closed = False
        self.generation = 0

    def _require_idle(self):
        if self.closed or self.loading or self.worker is not None and self.worker.is_alive():
            raise RuntimeError('请先停止当前仿真，等待结束后再操作')

    def start(self, policy='sine', duration=10):
        if policy not in ('sine', 'hold') or not 1 <= number(duration) <= 60:
            raise ValueError('请选择内置仿真策略，时长为 1–60 秒')
        from openarm_policy.robots import SimulatedRobot
        from openarm_policy.session import PolicySession, SessionConfig
        with self.lock:
            self._require_idle()
            self.generation += 1
            self.trace = PolicyTrace('仿真 · J1 正弦' if policy == 'sine' else '仿真 · 保持姿态')
            self.session = PolicySession(SimulatedRobot(), PreviewSinePolicy() if policy == 'sine' else HoldPolicy(),
                                         config=SessionConfig(reset_timeout_s=5), recorder=self.trace)
            session, trace = self.session, self.trace
            def run():
                try:
                    session.run(duration)
                except Exception as exc:
                    trace.record(dict(event='ended', state='failed', error=str(exc)))
            self.worker = threading.Thread(target=run, name='openarm-policy-preview', daemon=True)
            self.worker.start()

    def stop(self):
        with self.lock:
            if self.session is not None:
                self.session.stop()

    def close(self):
        with self.lock:
            self.closed = True
            if self.session is not None:
                self.session.stop()
            worker = self.worker
        if worker is not None:
            worker.join(timeout=10)
            if worker.is_alive():
                raise RuntimeError('仿真退出超时')

    def snapshot(self):
        with self.lock:
            trace = self.trace
            generation = self.generation
            busy = self.loading or self.worker is not None and self.worker.is_alive()
        return dict(trace.snapshot(), busy=bool(busy), generation=generation)

    def episodes(self):
        if not self.record_root.is_dir():
            return []
        return sorted(p.name for p in self.record_root.iterdir()
                      if p.is_dir() and not p.is_symlink() and (p/'events.jsonl').is_file())

    def load(self, name):
        with self.lock:
            self._require_idle()
            self.loading = True
        try:
            root = (self.record_root / name).resolve()
            if not name or root.parent != self.record_root or name != root.name:
                raise ValueError('请选择记录目录中的一个会话')
            path = (root/'events.jsonl').resolve()
            if path.parent != root or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
                raise ValueError('记录路径无效，或超过 64 MiB 预览上限')
            trace = PolicyTrace('记录回放 · ' + name)
            size = 0
            with path.open() as handle:
                for line in handle:
                    size += len(line.encode())
                    if size > 64 * 1024 * 1024:
                        raise ValueError('记录超过 64 MiB 预览上限')
                    if len(line) > 2 * 1024 * 1024:
                        raise ValueError('单条记录过大')
                    event = json.loads(line)
                    if event.get('event') == 'observation':
                        for key, history in event['observation']['streams'].items():
                            if not key.startswith('robot/'):
                                continue
                            # Load only robot state matrices, never camera/tactile arrays.
                            for sample in history[-1:]:
                                data = sample['data']
                                if isinstance(data, dict) and set(data) == {'array'}:
                                    array = (root/data['array']).resolve()
                                    if array.parent != root or array.stat().st_size > 4096:
                                        raise ValueError('关节状态数组路径或大小无效')
                                    import numpy as np
                                    values = np.load(array, allow_pickle=False)
                                    if values.shape != (8, 3):
                                        raise ValueError('关节状态数组必须为 8 × 3')
                                    sample['data'] = values.tolist()
                    trace.record(event)
            if not trace.names or not trace.points:
                raise ValueError('记录缺少动作上下文或关节采样')
            if trace.state in ('starting', 'running'):
                trace.state = 'incomplete'
            with self.lock:
                if self.closed:
                    raise RuntimeError('页面服务已关闭')
                self.trace = trace
                self.generation += 1
        except (KeyError, TypeError, IndexError) as exc:
            raise ValueError(f'记录结构无效：{exc}') from exc
        finally:
            with self.lock:
                self.loading = False
