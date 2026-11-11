"""fsutil: atomic writes, the cross-process file lock, and pid locks (live, stale, reused PID, age limit)."""
import json, os, subprocess, sys, threading
from pathlib import Path

import pytest

import fsutil

ROOT = Path(__file__).resolve().parents[1]
CHILD_TIMEOUT_S = 15

# Child process helpers run from the repo root so `import fsutil` resolves.
LOCK_HOLDER = """
import sys, time
import fsutil
with fsutil.file_lock(sys.argv[1]):
    print("held", flush=True)
    time.sleep(120)
"""

PID_TAKER = """
import sys, time
import fsutil
print("won" if fsutil.pid_lock_take(sys.argv[1], 1800) else "lost", flush=True)
time.sleep(120)
"""


def spawn(code, *args):
    return subprocess.Popen([sys.executable, "-c", code, *args], cwd=ROOT, stdout=subprocess.PIPE, text=True)


def read_line(proc):
    """First stdout line of a child; the child is killed if it does not answer in time."""
    timer = threading.Timer(CHILD_TIMEOUT_S, proc.kill)
    timer.start()
    try:
        return proc.stdout.readline().strip()
    finally:
        timer.cancel()


def stop(proc):
    proc.kill()
    proc.wait()
    proc.stdout.close()


# ---- write_json_atomic / write_atomic

def test_json_round_trips_and_replaces_previous_content(tmp_path):
    path = tmp_path / "state.json"
    fsutil.write_json_atomic(str(path), {"mode": "active", "name": "Jürgen"}, indent=2)
    fsutil.write_json_atomic(str(path), {"mode": "paused", "pausedAgents": ["a-1"]})
    assert json.loads(path.read_text(encoding="utf-8")) == {"mode": "paused", "pausedAgents": ["a-1"]}


def test_failed_serialisation_keeps_the_old_file_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "state.json"
    fsutil.write_json_atomic(str(path), {"mode": "active"})
    with pytest.raises(TypeError):
        fsutil.write_json_atomic(str(path), {"bad": object()})
    assert json.loads(path.read_text(encoding="utf-8")) == {"mode": "active"}
    assert os.listdir(tmp_path) == ["state.json"]


def test_bytes_are_written_unchanged(tmp_path):
    path = tmp_path / "blob.bin"
    fsutil.write_atomic(str(path), b"\x00\x01\r\n\xff")
    assert path.read_bytes() == b"\x00\x01\r\n\xff"


def test_replace_waits_for_a_reader_that_still_holds_the_file(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("old", encoding="utf-8")
    reader = open(path, "rb")  # Windows refuses to replace a file another handle has open
    release = threading.Timer(0.3, reader.close)
    release.start()
    try:
        fsutil.write_atomic(str(path), "new")
    finally:
        release.join()
    assert path.read_text(encoding="utf-8") == "new"


def test_replace_that_never_succeeds_raises_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(fsutil, "REPLACE_TRIES", 3)
    monkeypatch.setattr(fsutil, "REPLACE_WAIT_S", 0.01)
    path = tmp_path / "status.json"
    path.write_text("old", encoding="utf-8")
    with open(path, "rb"):
        with pytest.raises(PermissionError):
            fsutil.write_atomic(str(path), "new")
    assert path.read_text(encoding="utf-8") == "old"
    assert os.listdir(tmp_path) == ["status.json"]


# ---- file_lock

def test_file_lock_refuses_while_another_process_holds_it(tmp_path):
    lock = str(tmp_path / "data.lock")
    holder = spawn(LOCK_HOLDER, lock)
    try:
        assert read_line(holder) == "held"
        with pytest.raises(TimeoutError):
            with fsutil.file_lock(lock, timeout=0.3):
                pass
    finally:
        stop(holder)
    with fsutil.file_lock(lock, timeout=5):  # the OS dropped the lock with its holder
        pass


def test_file_lock_is_released_when_its_block_raises(tmp_path):
    lock = str(tmp_path / "data.lock")
    with pytest.raises(RuntimeError):
        with fsutil.file_lock(lock):
            raise RuntimeError("boom")
    with fsutil.file_lock(lock, timeout=0.5):
        pass


# ---- pid locks

def lock_path(tmp_path):
    return str(tmp_path / "tick.lock")


def write_holder(path, pid, start=None):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pid": pid, "start": start}, f)


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_live_holder_blocks_a_second_taker_until_it_releases(tmp_path):
    path = lock_path(tmp_path)
    assert fsutil.pid_lock_take(path, 1800) is True
    assert fsutil.pid_lock_take(path, 1800) is False
    fsutil.pid_lock_release(path)
    assert not os.path.exists(path)
    assert fsutil.pid_lock_take(path, 1800) is True


def test_release_leaves_a_lock_that_belongs_to_someone_else(tmp_path):
    path = lock_path(tmp_path)
    write_holder(path, dead_pid())
    fsutil.pid_lock_release(path)
    assert os.path.exists(path)


def test_lock_of_an_exited_holder_is_stale_at_once(tmp_path):
    path = lock_path(tmp_path)
    write_holder(path, dead_pid())
    assert fsutil.pid_lock_take(path, 1800) is True
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["pid"] == os.getpid()


def test_reused_pid_with_another_start_time_counts_as_dead(tmp_path):
    path = lock_path(tmp_path)
    write_holder(path, os.getpid(), start=1)  # this PID runs, but not as the process that took the lock
    assert fsutil.pid_lock_take(path, 1800) is True


def test_live_holder_past_the_age_limit_is_taken_over(tmp_path):
    path = lock_path(tmp_path)
    fsutil.pid_lock_write(path)  # this process is alive and holds the lock
    assert fsutil.pid_lock_take(path, 1800) is False
    old = os.stat(path).st_mtime - 3600
    os.utime(path, (old, old))  # the holder stopped refreshing the lock an hour ago
    assert fsutil.pid_lock_take(path, 1800) is True


def test_legacy_bare_pid_file_is_read_as_a_live_holder(tmp_path):
    path = lock_path(tmp_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    assert fsutil.pid_lock_take(path, 1800) is False


def test_unreadable_lock_file_is_stale(tmp_path):
    path = lock_path(tmp_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write("not a lock")
    assert fsutil.pid_lock_take(path, 1800) is True


def test_simultaneous_takers_of_a_stale_lock_produce_exactly_one_winner(tmp_path):
    path = lock_path(tmp_path)
    write_holder(path, dead_pid())
    takers = [spawn(PID_TAKER, path) for _ in range(4)]
    try:
        results = [read_line(t) for t in takers]
    finally:
        for t in takers:
            stop(t)
    assert sorted(results) == ["lost", "lost", "lost", "won"]
