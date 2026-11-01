"""One bounded in-flight inference, isolated from control and forcibly stoppable."""
import multiprocessing as mp
import time
import traceback


def _worker(connection, policy, context):
    try:
        policy.reset(context)
        connection.send(("ready", None))
        while True:
            observation = connection.recv()
            if observation is None:
                break
            connection.send(("action", policy.infer(observation)))
    except EOFError:
        pass
    except BaseException:
        try:
            connection.send(("error", traceback.format_exc()))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        try:
            policy.close()
        finally:
            connection.close()


class InferenceWorker:
    def __init__(self, policy, context):
        ctx = mp.get_context("spawn")
        self.connection, child = ctx.Pipe()
        self.process = ctx.Process(target=_worker, args=(child, policy, context), daemon=True)
        try:
            self.process.start()
        except BaseException:
            self.connection.close()
            raise
        finally:
            child.close()

    def ready(self, timeout, cancelled=None):
        deadline = time.monotonic() + timeout
        while not self.connection.poll(min(.05, max(0, deadline - time.monotonic()))):
            if cancelled is not None and cancelled():
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Policy reset timed out")
            if not self.process.is_alive():
                raise RuntimeError("Policy exited during reset")
        kind, message = self.connection.recv()
        if kind != "ready":
            raise RuntimeError(f"Policy reset failed: {message}")

    def submit(self, observation):
        self.connection.send(observation)

    def result(self):
        if self.connection.poll():
            kind, value = self.connection.recv()
            if kind != "action":
                raise RuntimeError(f"Policy failed: {value}")
            return value
        if not self.process.is_alive():
            raise RuntimeError("Policy process exited")
        return None

    def close(self):
        # Do not block sending into a pipe whose model is stuck in infer().
        if self.process.is_alive():
            try:
                self.connection.send(None)
            except (BrokenPipeError, EOFError, OSError):
                pass
            self.process.join(0.5)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(1)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(1)
        self.connection.close()
        if self.process.is_alive():
            raise RuntimeError("Policy process did not stop")
