from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
from queue import Empty
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from repoctl.scanner import core
from repoctl.scanner.core import (
    StateArtifactLockBusyError,
    StateArtifactLockError,
    StateArtifactLockOrderError,
    state_artifact_lock,
    state_artifact_lock_held,
)


def _hold_state_artifact_lock(state_root: str, acquired, release) -> None:
    with state_artifact_lock(state_root):
        acquired.set()
        release.wait(10)


def _hold_milestone_state_lock(
    state_root: str,
    milestone_id: str,
    attempting,
    acquired,
    release=None,
) -> None:
    attempting.set()
    with state_artifact_lock(state_root):
        acquired.put(milestone_id)
        if release is not None:
            release.wait(10)


class StateArtifactLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_persistent_lock_file_is_private_stable_and_nested_without_reflocking(self) -> None:
        lock_directory = self.root / "state" / ".locks"
        lock_path = lock_directory / "state-artifacts.lock"
        original_flock = core.fcntl.flock
        exclusive_acquisitions = []

        def count_exclusive_acquisitions(descriptor, operation):
            if operation & core.fcntl.LOCK_EX:
                exclusive_acquisitions.append(descriptor)
            return original_flock(descriptor, operation)

        with patch("repoctl.scanner.core.fcntl.flock", side_effect=count_exclusive_acquisitions):
            with state_artifact_lock(self.root / "state"):
                self.assertTrue(state_artifact_lock_held())
                lock_info = lock_path.stat()
                directory_info = lock_directory.stat()
                self.assertEqual(stat.S_IMODE(lock_info.st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(directory_info.st_mode), 0o700)
                self.assertEqual(lock_info.st_uid, os.geteuid())
                with state_artifact_lock(self.root / "state"):
                    self.assertTrue(state_artifact_lock_held())
                    self.assertEqual(lock_path.stat().st_ino, lock_info.st_ino)

        self.assertEqual(len(exclusive_acquisitions), 1)
        self.assertEqual(len(set(exclusive_acquisitions)), 1)
        self.assertFalse(state_artifact_lock_held())
        with state_artifact_lock(self.root / "state"):
            self.assertEqual(lock_path.stat().st_ino, lock_info.st_ino)

    def test_nested_different_state_root_is_rejected(self) -> None:
        with state_artifact_lock(self.root / "first"):
            with self.assertRaises(StateArtifactLockOrderError):
                with state_artifact_lock(self.root / "second"):
                    self.fail("different-root nested lock should not be acquired")

    def test_threads_serialize_on_the_same_state_root(self) -> None:
        started = threading.Event()
        acquired = threading.Event()

        def wait_for_lock() -> None:
            started.set()
            with state_artifact_lock(self.root / "state"):
                acquired.set()

        with state_artifact_lock(self.root / "state"):
            thread = threading.Thread(target=wait_for_lock)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(acquired.wait(0.05))
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(acquired.is_set())

    def test_thread_lock_timeout_is_reported(self) -> None:
        started = threading.Event()
        outcome = []

        def try_lock() -> None:
            started.set()
            try:
                with state_artifact_lock(self.root / "state"):
                    outcome.append("entered")
            except StateArtifactLockBusyError as exc:
                outcome.append(str(exc))

        with state_artifact_lock(self.root / "state"):
            with patch("repoctl.scanner.core.STATE_ARTIFACT_LOCK_TIMEOUT_SECONDS", 0.05):
                thread = threading.Thread(target=try_lock)
                thread.start()
                self.assertTrue(started.wait(2))
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIn("busy", outcome[0])

    def test_symlink_lock_file_is_rejected(self) -> None:
        lock_directory = self.root / "state" / ".locks"
        lock_directory.mkdir(parents=True, mode=0o700)
        target = self.root / "target"
        target.write_text("not a lock", encoding="utf-8")
        (lock_directory / "state-artifacts.lock").symlink_to(target)

        with self.assertRaisesRegex(StateArtifactLockError, "safely open"):
            with state_artifact_lock(self.root / "state"):
                self.fail("symlink lock file should not be followed")

    def test_unsafe_lock_directory_permissions_are_rejected(self) -> None:
        lock_directory = self.root / "state" / ".locks"
        lock_directory.mkdir(parents=True, mode=0o700)
        lock_directory.chmod(0o755)

        with self.assertRaisesRegex(StateArtifactLockError, "unsafe type, ownership, or permissions"):
            with state_artifact_lock(self.root / "state"):
                self.fail("unsafe lock directory should be rejected")

    def test_lock_timeout_is_reported_without_entering_critical_section(self) -> None:
        context = multiprocessing.get_context("fork")
        acquired = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_state_artifact_lock,
            args=(str(self.root / "state"), acquired, release),
        )
        process.start()
        try:
            self.assertTrue(acquired.wait(5), "lock holder did not acquire the state lock")
            with patch("repoctl.scanner.core.STATE_ARTIFACT_LOCK_TIMEOUT_SECONDS", 0.05):
                with self.assertRaisesRegex(StateArtifactLockBusyError, "busy"):
                    with state_artifact_lock(self.root / "state"):
                        self.fail("timed-out caller must not enter the critical section")
        finally:
            release.set()
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertFalse(process.is_alive())
        self.assertEqual(process.exitcode, 0)

    def test_process_termination_releases_the_persistent_lock(self) -> None:
        context = multiprocessing.get_context("fork")
        acquired = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_state_artifact_lock,
            args=(str(self.root / "state"), acquired, release),
        )
        process.start()
        try:
            self.assertTrue(acquired.wait(5), "lock holder did not acquire the state lock")
        finally:
            if process.is_alive():
                process.terminate()
            process.join(5)
        self.assertFalse(process.is_alive())
        with state_artifact_lock(self.root / "state"):
            self.assertTrue(state_artifact_lock_held())

    def test_different_milestones_share_one_cross_process_state_root_lock(self) -> None:
        context = multiprocessing.get_context("fork")
        first_attempting = context.Event()
        second_attempting = context.Event()
        first_acquired = context.Queue()
        second_acquired = context.Queue()
        first_release = context.Event()
        first = context.Process(
            target=_hold_milestone_state_lock,
            args=(str(self.root / "state"), "M003.001", first_attempting, first_acquired, first_release),
        )
        second = context.Process(
            target=_hold_milestone_state_lock,
            args=(str(self.root / "state"), "M003.002", second_attempting, second_acquired),
        )
        first.start()
        second_started = False
        first_id = None
        second_id = None
        try:
            self.assertTrue(first_attempting.wait(5), "first milestone did not acquire the state lock")
            first_id = first_acquired.get(timeout=5)
            second.start()
            second_started = True
            self.assertTrue(second_attempting.wait(5), "second milestone did not attempt the state lock")
            with self.assertRaises(Empty):
                second_acquired.get(timeout=0.05)
            first_release.set()
            second_id = second_acquired.get(timeout=5)
        finally:
            first_release.set()
            first.join(5)
            processes = [first]
            if second_started:
                second.join(5)
                processes.append(second)
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
        self.assertFalse(first.is_alive())
        if second_started:
            self.assertFalse(second.is_alive())
        self.assertEqual(first_id, "M003.001")
        self.assertEqual(second_id, "M003.002")


if __name__ == "__main__":
    unittest.main()
