"""Bounded argv-only command execution with a predictable system PATH."""
import os
import contextlib
import selectors
import signal
import subprocess
import time

TRUSTED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
OUTPUT_LIMIT = 2 * 1024 * 1024

class Runner:
    """Arguments only; command output stays local, never automatically logged."""
    def run(self, args, check=False, timeout=30):
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise TypeError('command must be a list of strings')
        environment = dict(os.environ, PATH=TRUSTED_PATH)
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, env=environment, start_new_session=True)
        output = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(args, timeout)
                    for key, _ in selector.select(min(remaining, 0.5)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output[key.data].extend(chunk)
                        if len(output[key.data]) > OUTPUT_LIMIT:
                            raise OSError("Command output exceeded the 2 MiB safety limit")
            returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        finally:
            process.stdout.close()
            process.stderr.close()
        result = subprocess.CompletedProcess(args, returncode,
            output["stdout"].decode("utf-8", errors="replace"),
            output["stderr"].decode("utf-8", errors="replace"))
        if check:
            result.check_returncode()
        return result
