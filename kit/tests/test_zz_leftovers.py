"""KF-59 (0.6.2): no process the suite started outlives it.

Discovery runs modules in name order, so this one runs last. It reads `ps` once and keeps only the processes that
descend from this test process (its children and theirs): nothing else on the machine is looked at or signalled.
What it finds it stops, so a failing run leaks nothing either, and it fails naming them. A process whose parent died
was handed to the system's first process and no longer descends from the suite; the tests stop their own process
groups for that reason (test_lock's `start`).
"""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import os
import signal
import subprocess
import time
import unittest

GRACE = 10.0  # seconds a process that is ending anyway gets before it counts as left behind


def descendants(root: int) -> dict[int, str]:
    """{pid: command} of the live processes below `root` (zombies and this call's own `ps` left out)."""
    proc = subprocess.Popen(["ps", "-A", "-ww", "-o", "pid=,ppid=,stat=,command="], stdout=subprocess.PIPE,
                            text=True)
    out, _ = proc.communicate(timeout=30)
    children: dict[int, list[tuple[int, str, str]]] = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        children.setdefault(int(parts[1]), []).append((int(parts[0]), parts[2], parts[3] if len(parts) > 3 else ""))
    found, todo = {}, [root]
    while todo:
        for pid, stat, command in children.get(todo.pop(), []):
            if pid == proc.pid or pid in found:
                continue
            todo.append(pid)
            if not stat.startswith("Z"):
                found[pid] = command
    return found


class NoLeftoverProcesses(unittest.TestCase):
    def test_a_grandchild_is_found_and_the_runner_itself_is_not(self):
        child = subprocess.Popen(["sh", "-c", "sleep 30 & wait"], start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            found = {}
            while time.monotonic() < deadline and len(found) < 2:
                found = descendants(os.getpid())
                time.sleep(0.05)
            self.assertIn(child.pid, found)
            self.assertTrue(any(command.startswith("sleep 30") for command in found.values()), found)
            self.assertNotIn(os.getpid(), found)
            self.assertNotIn(os.getppid(), found)
        finally:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()

    def test_no_process_the_suite_started_is_still_running(self):
        deadline = time.monotonic() + GRACE
        left = descendants(os.getpid())
        while left and time.monotonic() < deadline:
            time.sleep(0.2)
            left = descendants(os.getpid())
        for pid in left:  # ours: stop them, so this run leaks nothing whatever it reports
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.assertEqual(left, {}, "processes the kit's tests started are still running (stopped now)")


if __name__ == "__main__":
    unittest.main()
