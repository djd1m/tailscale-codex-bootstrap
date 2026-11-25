import json
import multiprocessing
from pathlib import Path
import stat
import tempfile
import unittest

from tailnet_bootstrap.state import StateError, StateStore


def increment(directory, times):
    state = StateStore(directory)
    for _ in range(times):
        state.update(lambda data: data.update(count=data.get("count", 0) + 1))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name) / "private")

    def test_atomic_private_roundtrip(self):
        self.assertEqual(self.store.load(), {})
        self.store.save({"step": "ready", "count": 1})
        self.assertEqual(self.store.load()["step"], "ready")
        self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.lock_path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.store.directory.iterdir()), ["state.json", "state.lock"])

    def test_secret_keys_and_nonfinite_data_are_rejected(self):
        for data in ({"api_key": "private"}, {"accessToken": "private"}, {"auth_key": "private"}, {"nested": [{"access_token": "private"}]}, {"password": "private"}, {"x": float("nan")}):
            with self.subTest(data=data):
                with self.assertRaises(StateError):
                    self.store.save(data)
        self.assertEqual(self.store.load(), {})

    def test_failed_save_does_not_replace_valid_state(self):
        self.store.save({"status": "good"})
        with self.assertRaises(StateError):
            self.store.save({"secret": "bad"})
        self.assertEqual(self.store.load(), {"status": "good"})

    def test_corrupt_state_is_not_silently_reset(self):
        self.store.path.write_text("not JSON")
        with self.assertRaises(StateError):
            self.store.load()
        self.assertEqual(self.store.path.read_text(), "not JSON")

    def test_state_and_lock_symlinks_are_refused(self):
        outside = Path(self.temp.name) / "outside"
        outside.write_text('{"safe":true}')
        self.store.path.symlink_to(outside)
        with self.assertRaises(StateError):
            self.store.load()
        self.store.path.unlink()
        self.store.lock_path.unlink()
        self.store.lock_path.symlink_to(outside)
        with self.assertRaises(StateError):
            self.store.save({"x": 1})
        self.assertEqual(json.loads(outside.read_text()), {"safe": True})

    def test_symlink_directory_is_refused(self):
        link = Path(self.temp.name) / "link"
        link.symlink_to(self.store.directory, target_is_directory=True)
        with self.assertRaises(StateError):
            StateStore(link)

    def test_flock_protects_process_read_modify_write(self):
        self.store.save({"count": 0})
        jobs = [multiprocessing.Process(target=increment, args=(str(self.store.directory), 20)) for _ in range(3)]
        for job in jobs:
            job.start()
        for job in jobs:
            job.join(15)
            self.assertEqual(job.exitcode, 0)
        self.assertEqual(self.store.load()["count"], 60)

    def test_transaction_checkpoint_survives_external_operation_failure(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as transaction:
                transaction.data["step"] = "pending"
                transaction.checkpoint()
                transaction.data["step"] = "not yet durable"
                raise RuntimeError("simulated process interruption")
        self.assertEqual(self.store.load(), {"step": "pending"})


if __name__ == "__main__":
    unittest.main()
