"""Small operator controls embedded in the existing teleoperation page."""
import time
import math


def attach(server, teleop, collection):
    with server.gui.add_folder("演示数据采集"):
        server.gui.add_markdown("开始跟随后录制。结束录制保持跟随；停止跟随仍会失能。每段验收后可继续下一段。")
        task = server.gui.add_text("任务", initial_value="")
        operator = server.gui.add_text("操作者", initial_value="")
        start = server.gui.add_button("开始录制", color="green")
        end = server.gui.add_button("结束录制")
        outcome = server.gui.add_dropdown("任务结果", options=("成功", "失败", "中断"))
        note = server.gui.add_text("备注", initial_value="")
        accept = server.gui.add_button("保存 · 下一段")
        reject = server.gui.add_button("弃用 · 重采")
        status = server.gui.add_markdown(collection.message)
        tactile_status = server.gui.add_markdown("")
        with server.gui.add_folder("采集详情", expand_by_default=False):
            details = server.gui.add_markdown("")
    images = {}
    labels = {"camera/head": "头部", "camera/left": "左腕", "camera/right": "右腕"}
    for name, spec in collection.streams.items():
        if name.startswith("camera/") and spec.get("encoding") == "bgr8":
            import numpy as np
            images[name] = (server.gui.add_image(np.zeros((2, 2, 3), dtype=np.uint8),
                            label=labels.get(name, name), format="jpeg", jpeg_quality=75), -1)
    previous_counts, previous_time = {}, time.monotonic()

    def action(event, operation):
        if event.client is None:
            return
        if teleop.owner is not None and event.client.client_id != teleop.owner:
            return
        try:
            operation()
        except Exception as exc:
            collection.message = str(exc)

    start.on_click(lambda event: action(event, lambda: collection.start(task.value, operator.value)))
    end.on_click(lambda event: action(event, collection.finish))
    outcomes = {"成功": "success", "失败": "failure", "中断": "interrupted"}
    accept.on_click(lambda event: action(event, lambda: collection.review(outcomes[outcome.value], True, note.value)))
    reject.on_click(lambda event: action(event, lambda: collection.review(outcomes[outcome.value], False, note.value)))

    def refresh():
        nonlocal previous_counts, previous_time
        status.content = collection.message
        recording = collection.episode is not None
        episode = collection.episode
        if episode:
            status.content += (f"\n\n{time.monotonic() - episode.manifest['started_at']:.1f} 秒"
                               f" · 待写 {episode.queue.qsize()} 条 · {episode.bytes / 1024**2:.1f} MiB")
        now = time.monotonic()
        with collection.lock:
            specs, counts, latest_times = dict(collection.streams), dict(collection.sequence), dict(collection.latest)
        online = [name for name, spec in specs.items()
                  if 0 <= now - latest_times.get(name, 0) <= spec['max_age_s']]
        status.content += f"\n\n数据流 {len(online)}/{len(specs)} 在线"
        tactile_status.content = "\n\n".join(
            f"{name.split('/')[-1]} · XYZ原始值 " + " / ".join(f"{v:.0f}" for v in values)
            + (" · 已过期" if name not in online else "")
            for name, values in tuple(collection.tactile_values.items()))
        rows = ["| 数据流 | 更新 Hz | 延迟 ms |", "|---|---:|---:|"]
        for name in specs:
            hz = (counts.get(name, 0) - previous_counts.get(name, counts.get(name, 0))) / max(.001, now - previous_time)
            age = f"{1000 * (now - latest_times[name]):.0f}" if name in latest_times else "—"
            rows.append(f"| {labels.get(name, name)} {'✓' if name in online else '等待'} | {hz:.1f} | {age} |")
        details.content = "\n".join(rows)
        previous_counts, previous_time = counts, now
        start.disabled = recording or collection.last is not None or not collection.ready
        end.disabled = not recording
        accept.disabled = reject.disabled = collection.last is None
        task.disabled = operator.disabled = recording
        for name, (handle, previous) in tuple(images.items()):
            latest = collection.previews.get(name)
            if latest and latest[1] != previous:
                image = latest[0].image
                step = max(1, math.ceil(image.shape[1] / 480))
                handle.image = image[::step, ::step, ::-1].copy()
                images[name] = (handle, latest[1])
    return refresh
