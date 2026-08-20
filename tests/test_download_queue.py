import threading
import time

from download_queue import DownloadTask, DownloadQueue


def make_task(chat_id, tdl_name, kind="single"):
    return DownloadTask(task_id=0, chat_id=chat_id, tdl_name=tdl_name, kind=kind)


def wait_until(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeProcess:
    def __init__(self):
        self.killed = False

    def kill(self):
        self.killed = True

    def poll(self):
        return 0 if self.killed else None


def test_global_limit_and_queue():
    started = []
    release = threading.Event()

    def worker(task):
        started.append(task.task_id)
        release.wait(2.0)
        queue._finish(task)

    queue = DownloadQueue(2, worker)
    for i in range(5):
        queue.submit(make_task(1, f"a{i}"))

    assert wait_until(lambda: len(started) >= 2)
    assert queue.counts() == {"running": 2, "pending": 3}
    release.set()
    assert wait_until(lambda: queue.counts()["running"] == 0)


def test_same_account_serialized():
    started = []
    release = threading.Event()

    def worker(task):
        started.append(task.task_id)
        release.wait(2.0)
        queue._finish(task)

    queue = DownloadQueue(5, worker)
    queue.submit(make_task(1, "acc1"))
    queue.submit(make_task(1, "acc1"))
    queue.submit(make_task(2, "acc2"))

    assert wait_until(lambda: len(started) >= 2)
    assert queue.counts() == {"running": 2, "pending": 1}
    release.set()
    assert wait_until(lambda: queue.counts()["running"] == 0)


def test_cancel_all():
    release = threading.Event()
    procs = {}

    def worker(task):
        task.process = FakeProcess()
        procs[task.task_id] = task.process
        release.wait(2.0)
        queue._finish(task)

    queue = DownloadQueue(2, worker)
    id1 = queue.submit(make_task(1, "a"))
    queue.submit(make_task(1, "a"))
    queue.submit(make_task(2, "b"))

    assert wait_until(lambda: len(procs) >= 2)
    assert queue.counts() == {"running": 2, "pending": 1}

    n = queue.cancel_all(chat_id=1)
    assert n == 2  # 1 个运行中 + 1 个排队
    assert procs[id1].killed is True
    assert queue.counts() == {"running": 2, "pending": 0}
    release.set()
    assert wait_until(lambda: queue.counts()["running"] == 0)
