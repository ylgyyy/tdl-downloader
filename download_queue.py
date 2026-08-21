"""下载任务队列：全局并发上限 + 同账号串行调度。

只依赖标准库，方便独立单元测试（不依赖 telebot / tdl / pexpect）。
"""
import threading
import collections
from dataclasses import dataclass


@dataclass
class DownloadTask:
    task_id: int
    chat_id: int
    tdl_name: str
    kind: str  # "single" | "multi"

    # single 参数
    channel_id: str = None
    msg_id: str = None
    link: str = None
    rename_name: str = None

    # multi 参数
    source_id: str = None
    source_link: str = None
    start_id: str = None
    end_id: str = None

    # 运行时状态（由队列 / worker 填充）
    process: object = None       # subprocess.Popen，dl 阶段设置
    export_file: str = None
    dl_dir: str = None
    step_msg_id: int = None
    status: str = "queued"       # queued / running / done / failed / canceled
    canceled: bool = False
    before_files: object = None  # 下载前目录快照（set）


class DownloadQueue:
    """按全局并发上限调度任务，同一 tdl 账号串行执行。"""

    def __init__(self, max_concurrent, worker):
        self.max = max(1, int(max_concurrent))
        self.worker = worker  # callable(task) -> 执行整个下载流程
        self.lock = threading.Lock()
        self.pending = collections.deque()
        self.running = {}          # task_id -> DownloadTask
        self.busy_accounts = set()  # 正在运行的账号名
        self._next_id = 1

    def submit(self, task):
        """入队并尝试调度，返回 task_id。"""
        with self.lock:
            task.task_id = self._next_id
            self._next_id += 1
            task.status = "queued"
            self.pending.append(task)
            self._pump_locked()
        return task.task_id

    def _pump_locked(self):
        """在 self.lock 内调用：启动尽可能多的任务。"""
        remaining = collections.deque()
        to_start = []
        while self.pending:
            task = self.pending.popleft()
            if len(self.running) >= self.max:
                remaining.append(task)
                continue
            if task.tdl_name in self.busy_accounts:
                remaining.append(task)  # 同账号串行，跳过
                continue
            # 必须在循环内同步更新状态，否则后面排队任务看不到已被占用的并发额度/账号
            self.running[task.task_id] = task
            self.busy_accounts.add(task.tdl_name)
            task.status = "running"
            to_start.append(task)
        self.pending = remaining
        # Thread.start() 非阻塞，锁内启动安全
        for task in to_start:
            threading.Thread(target=self.worker, args=(task,), daemon=True).start()

    def _finish(self, task):
        """worker 结束时调用：释放资源并调度下一个。"""
        with self.lock:
            self.running.pop(task.task_id, None)
            self.busy_accounts.discard(task.tdl_name)
            self._pump_locked()

    def cancel_all(self, chat_id):
        """取消该用户全部任务（排队 + 运行中），返回取消数。"""
        with self.lock:
            canceled = 0
            remaining = collections.deque()
            while self.pending:
                t = self.pending.popleft()
                if t.chat_id == chat_id:
                    t.status = "canceled"
                    canceled += 1
                else:
                    remaining.append(t)
            self.pending = remaining
            for t in list(self.running.values()):
                if t.chat_id == chat_id:
                    t.canceled = True
                    if t.process is not None:
                        try:
                            t.process.kill()
                        except Exception:
                            pass
                    canceled += 1
            return canceled

    def running_pids(self):
        """当前运行中下载进程的 pid 集合（供 _kill_stale_tdl 保护）。"""
        with self.lock:
            pids = set()
            for t in self.running.values():
                if t.process is not None:
                    try:
                        if t.process.poll() is None:
                            pids.add(t.process.pid)
                    except Exception:
                        pass
            return pids

    def counts(self):
        """返回 {"running": n, "pending": n}，便于测试与文案。"""
        with self.lock:
            return {"running": len(self.running), "pending": len(self.pending)}

    def status_text(self, chat_id):
        """给用户的队列反馈文案。"""
        with self.lock:
            mine_pending = sum(1 for t in self.pending if t.chat_id == chat_id)
            running_total = len(self.running)
        return f"排队 {mine_pending} 个 · 正在下载 {running_total} 个"
