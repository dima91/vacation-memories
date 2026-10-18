import json
import os
import socket
import stat
import tempfile

import pytest

from vacation_memories.workdir import WorkdirInUseError, WorkdirLock, prepare_workdir


class TestPrepareWorkdir:
    def test_creates_the_workdir_private_with_a_tmp_folder(self, tmp_path):
        workdir = tmp_path / "work"
        paths = prepare_workdir(workdir)
        assert paths.root == workdir
        assert paths.tmp == workdir / "tmp"
        assert stat.S_IMODE(workdir.stat().st_mode) == 0o700
        assert stat.S_IMODE(paths.tmp.stat().st_mode) == 0o700

    def test_tightens_the_permissions_of_an_existing_workdir(self, tmp_path):
        workdir = tmp_path / "work"
        workdir.mkdir(mode=0o755)
        prepare_workdir(workdir)
        assert stat.S_IMODE(workdir.stat().st_mode) == 0o700

    def test_temporary_files_are_created_in_the_workdir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", "/nonexistent")
        monkeypatch.setattr(tempfile, "tempdir", None)
        paths = prepare_workdir(tmp_path / "work")
        assert os.environ["TMPDIR"] == str(paths.tmp)
        with tempfile.NamedTemporaryFile() as handle:
            assert os.path.dirname(handle.name) == str(paths.tmp)

    def test_unusable_workdir_is_an_error(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        with pytest.raises(OSError):
            prepare_workdir(blocker / "work")


class TestWorkdirLock:
    def test_writes_the_status_of_the_running_instance(self, tmp_path):
        with WorkdirLock(tmp_path):
            status = json.loads((tmp_path / "status.json").read_text())
        assert status["state"] == "running"
        assert status["pid"] == os.getpid()
        assert status["hostname"] == socket.gethostname()
        assert status["started_at"]

    def test_second_instance_on_the_same_workdir_is_refused_and_names_the_first(self, tmp_path):
        with WorkdirLock(tmp_path):
            with pytest.raises(WorkdirInUseError) as refused:
                WorkdirLock(tmp_path).__enter__()
        message = str(refused.value)
        assert socket.gethostname() in message
        assert str(os.getpid()) in message
        started_at = json.loads((tmp_path / "status.json").read_text())["started_at"]
        assert started_at in message

    def test_refusal_does_not_overwrite_the_status_of_the_first_instance(self, tmp_path):
        with WorkdirLock(tmp_path):
            before = (tmp_path / "status.json").read_text()
            with pytest.raises(WorkdirInUseError):
                WorkdirLock(tmp_path).__enter__()
            assert (tmp_path / "status.json").read_text() == before

    def test_failure_to_write_the_status_releases_the_lock(self, tmp_path, monkeypatch):
        def broken_start(self):
            raise OSError("disk full")

        with monkeypatch.context() as patch:
            patch.setattr("vacation_memories.workdir.StatusWriter.start", broken_start)
            with pytest.raises(OSError, match="disk full"):
                WorkdirLock(tmp_path).__enter__()
        with WorkdirLock(tmp_path):  # the failed attempt did not keep the lock
            pass

    def test_status_of_a_finished_run_is_not_reported_as_the_holder(self, tmp_path):
        first = WorkdirLock(tmp_path)
        with first:
            first.status.finish("done")
            with pytest.raises(WorkdirInUseError) as refused:
                WorkdirLock(tmp_path, holder_wait=0).__enter__()
        assert str(os.getpid()) not in str(refused.value)
        assert "another instance" in str(refused.value)

    def test_old_status_of_a_crashed_run_is_not_reported_as_the_holder(self, tmp_path):
        stale = {"pid": 4242, "hostname": "gone", "state": "running", "started_at": "2020-01-01T00:00:00+00:00",
                 "updated_at": "2020-01-01T00:00:00+00:00"}
        (tmp_path / "status.json").write_text(json.dumps(stale))
        import fcntl

        descriptor = os.open(tmp_path / ".lock", os.O_RDWR | os.O_CREAT)
        fcntl.flock(descriptor, fcntl.LOCK_EX)  # a holder that has not written its status yet
        try:
            with pytest.raises(WorkdirInUseError) as refused:
                WorkdirLock(tmp_path, holder_wait=0).__enter__()
        finally:
            os.close(descriptor)
        assert "4242" not in str(refused.value) and "gone" not in str(refused.value)

    def test_refused_instance_waits_briefly_for_the_holder_to_write_its_status(self, tmp_path):
        import fcntl
        import threading
        import time

        (tmp_path / "status.json").write_text("{}")
        descriptor = os.open(tmp_path / ".lock", os.O_RDWR | os.O_CREAT)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        fresh = {"pid": 777, "hostname": "box", "state": "running"}

        def holder_writes_status():
            time.sleep(0.2)
            now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
            (tmp_path / "status.json").write_text(json.dumps({**fresh, "started_at": now, "updated_at": now}))

        thread = threading.Thread(target=holder_writes_status)
        thread.start()
        try:
            with pytest.raises(WorkdirInUseError) as refused:
                WorkdirLock(tmp_path, holder_wait=3).__enter__()
        finally:
            thread.join()
            os.close(descriptor)
        assert "PID 777" in str(refused.value) and "box" in str(refused.value)

    def test_lock_is_released_on_exit(self, tmp_path):
        with WorkdirLock(tmp_path):
            pass
        with WorkdirLock(tmp_path):
            pass

    def test_different_workdirs_are_independent(self, tmp_path):
        first, second = tmp_path / "a", tmp_path / "b"
        first.mkdir()
        second.mkdir()
        with WorkdirLock(first):
            with WorkdirLock(second):
                pass

    def test_refusal_without_readable_status_still_reports_the_workdir_in_use(self, tmp_path):
        with WorkdirLock(tmp_path):
            (tmp_path / "status.json").write_text("{ not json")
            with pytest.raises(WorkdirInUseError, match="in use"):
                WorkdirLock(tmp_path, holder_wait=0).__enter__()

    def test_lock_dies_with_the_process(self, tmp_path):
        import subprocess
        import sys
        import textwrap

        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import sys, time
                    from vacation_memories.workdir import WorkdirLock
                    with WorkdirLock({str(tmp_path)!r}):
                        print("locked", flush=True)
                        time.sleep(60)
                    """
                ),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "locked"
            with pytest.raises(WorkdirInUseError) as refused:
                WorkdirLock(tmp_path).__enter__()
            assert str(holder.pid) in str(refused.value)
        finally:
            holder.kill()
            holder.wait()
        with WorkdirLock(tmp_path):  # no orphan lock after kill -9
            pass


class TestStatusWriter:
    def test_updates_are_atomic_and_keep_the_start_time(self, tmp_path):
        with WorkdirLock(tmp_path) as lock:
            started_at = lock.status.read()["started_at"]
            lock.status.update(phase="scan", done=3, total=10, eta_seconds=42.0)
            data = lock.status.read()
            assert (data["phase"], data["done"], data["total"], data["eta_seconds"]) == ("scan", 3, 10, 42.0)
            assert data["started_at"] == started_at
            assert data["updated_at"]
            lock.status.finish("done")
            assert lock.status.read()["state"] == "done"
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

    def test_rejects_an_unknown_state(self, tmp_path):
        with WorkdirLock(tmp_path) as lock:
            with pytest.raises(ValueError):
                lock.status.finish("exploded")
