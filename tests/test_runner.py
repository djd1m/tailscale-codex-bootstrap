import subprocess
import unittest
from tailnet_bootstrap.runner import Runner


class RunnerTests(unittest.TestCase):
    def test_separate_bounded_capture(self):
        result = Runner().run(["/usr/bin/python3", "-c", "import sys; print('ok'); print('err',file=sys.stderr)"])
        self.assertEqual(result.stdout, "ok\n")
        self.assertEqual(result.stderr, "err\n")

    def test_excessive_output_stops_process(self):
        with self.assertRaises(OSError):
            Runner().run(["/usr/bin/python3", "-c", "import sys; sys.stdout.write('x'*4000000)"], timeout=5)

    def test_timeout_kills_command(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            Runner().run(["/usr/bin/python3", "-c", "import time; time.sleep(10)"], timeout=0.1)

    def test_no_shell_strings(self):
        with self.assertRaises(TypeError):
            Runner().run("echo not-an-argv")

    def test_checked_exit_status(self):
        with self.assertRaises(subprocess.CalledProcessError):
            Runner().run(["/usr/bin/python3", "-c", "raise SystemExit(3)"], check=True)


if __name__ == "__main__":
    unittest.main()
