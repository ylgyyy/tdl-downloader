# 并发下载队列实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让机器人支持同一用户同时提交多个下载任务，按「全局并发上限 + 排队 + 同账号串行」调度，每个任务独立追踪与反馈。

**Architecture:** 新增独立模块 `download_queue.py` 存放纯调度逻辑（`DownloadTask` + `DownloadQueue`，只依赖标准库，可单测）；`tdl.py` 把下载执行合并成 `_run_task(task)` 作为队列的 worker，`do_single_download` / `do_multi_download` 只负责构建任务并入队。

**Tech Stack:** Python 3.11、标准库（dataclasses / threading / collections）、pytest（仅测试）。

**Spec:** `docs/superpowers/specs/2026-08-18-concurrent-download-queue-design.md`

---

## 文件结构

- **Create** `download_queue.py` — `DownloadTask` + `DownloadQueue`（纯标准库，无 telebot/tdl 依赖）
- **Create** `tests/test_download_queue.py` — 调度逻辑单测
- **Create** `requirements-dev.txt` — `pytest`
- **Modify** `.gitignore` — 加 `.pytest_cache/`
- **Modify** `tdl.py` — 接入队列、重构下载函数、改 `main_menu`/`cmd_cancel`/`handle_steps`
- **Modify** `docker-compose.yml` — 加 `MAX_CONCURRENT_DL`
- **Modify** `README.md`、`CHANGELOG.md` — 文档

---

## Task 1: 测试基础设施

**Files:**
- Create: `requirements-dev.txt`
- Modify: `.gitignore`

- [ ] **Step 1: 创建 `requirements-dev.txt`**

```text
pytest
```

- [ ] **Step 2: `.gitignore` 追加 pytest 缓存**

在 `# ============ Python ============` 段末尾（`*.egg-info/` 之后）加一行：

```gitignore
.pytest_cache/
```

- [ ] **Step 3: 安装 pytest**

Run: `cd "D:/Users/siila/Desktop/hub上传项目/tdl-downloader" && python -m pip install -r requirements-dev.txt`
Expected: 成功安装 pytest

- [ ] **Step 4: Commit**

```bash
git add requirements-dev.txt .gitignore
git commit -m "chore: 添加 pytest 测试基础设施"
```

---

## Task 2: `download_queue.py` 调度模块（TDD）

**Files:**
- Create: `tests/test_download_queue.py`
- Create: `download_queue.py`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_download_queue.py`：

```python
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
    for _ in range(5):
        queue.submit(make_task(1, "a"))

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
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd "D:/Users/siila/Desktop/hub上传项目/tdl-downloader" && python -m pytest tests/test_download_queue.py -v`
Expected: FAIL，报 `ModuleNotFoundError: No module named 'download_queue'`

- [ ] **Step 3: 实现 `download_queue.py`**

创建 `download_queue.py`：

```python
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
            to_start.append(task)
        self.pending = remaining
        for task in to_start:
            self.running[task.task_id] = task
            self.busy_accounts.add(task.tdl_name)
            task.status = "running"
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
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd "D:/Users/siila/Desktop/hub上传项目/tdl-downloader" && python -m pytest tests/test_download_queue.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add download_queue.py tests/test_download_queue.py
git commit -m "feat: 添加下载任务队列调度模块（全局上限 + 同账号串行）"
```

---

## Task 3: 重构 `tdl.py` 下载执行（`_run_task` + 新 `do_single_download` / `do_multi_download`）

**Files:**
- Modify: `tdl.py`

### 3.1 更新 imports

把文件顶部 import 段：

```python
import re
import json
import subprocess
import os
import threading
import telebot
import pexpect
from time import sleep
from functools import wraps
from telebot.types import BotCommand
```

改为：

```python
import re
import json
import subprocess
import os
import threading
import time
import telebot
import pexpect
from time import sleep
from functools import wraps
from telebot.types import BotCommand
from download_queue import DownloadTask, DownloadQueue
```

### 3.2 新增 `MAX_CONCURRENT_DL` 配置

在 `TDL_PROXY = os.environ.get("TDL_PROXY", "")` 这一行之后新增：

```python
MAX_CONCURRENT_DL = int(os.environ.get("MAX_CONCURRENT_DL", "2") or 2)
```

### 3.3 删除 `active_downloads` 全局

删除这两行（及其注释）：

```python
# 存储正在下载的子进程 {chat_id: subprocess.Popen}
active_downloads = {}
```

### 3.4 新增 `_cleanup_tmp` 辅助函数

在 `_format_file_list` 函数之后新增：

```python
def _cleanup_tmp(dl_dir):
    """删除目录中的 .tmp 残留文件"""
    try:
        for f in os.listdir(dl_dir):
            if f.endswith(".tmp"):
                os.remove(os.path.join(dl_dir, f))
    except Exception:
        pass
```

### 3.5 用 `_run_task` 替换 `_watch_single_download` / `do_single_download` / `_watch_multi_download`

删除 `_watch_single_download`（当前约 621-699 行）、`do_single_download`（当前约 736-791 行）、`_watch_multi_download`（当前约 794-867 行）三个函数，替换为下面的 `_run_task`、`do_single_download`、`do_multi_download` 和 `queue` 实例。注意：保留 `_rename_new_files` 函数不动。

```python
def _run_task(task):
    """在独立线程中执行一个下载任务：export → dl → 轮询进度 → 收尾。"""
    label = "单文件下载" if task.kind == "single" else "批量下载"

    # Step 1/2: 导出（先清理残留进程）
    _kill_stale_tdl()
    step_msg = bot.send_message(task.chat_id, f"📥 *{label}*\n\n📡 Step 1/2: 导出消息...", parse_mode="HTML")
    task.step_msg_id = step_msg.message_id

    if task.kind == "single":
        task.export_file = f"single-export_{task.chat_id}_{task.task_id}.json"
        export_argv = ["tdl", "-n", task.tdl_name, "chat", "export", "-c", task.channel_id,
                       "-i", f"{task.msg_id},{task.msg_id}", "-T", "id", "-o", task.export_file]
        total = 1
    else:
        task.export_file = f"dl-export_{task.chat_id}_{task.task_id}.json"
        export_argv = ["tdl", "-n", task.tdl_name, "chat", "export", "-c", task.source_id,
                       "-i", f"{task.start_id},{task.end_id}", "-T", "id", "-o", task.export_file]
        total = 1
    if TDL_PROXY:
        export_argv += ["--proxy", TDL_PROXY]
    success, _, stderr, _ = run_command(export_argv, task.chat_id)

    if task.canceled:
        queue._finish(task)
        return
    if not success:
        bot.edit_message_text(f"❌ 导出失败：{stderr[:200]}", task.chat_id, step_msg.message_id)
        queue._finish(task)
        return

    if task.kind == "multi":
        file_ok, file_msg = check_export_file(task.export_file)
        if not file_ok:
            bot.edit_message_text(f"❌ {file_msg}", task.chat_id, step_msg.message_id)
            if os.path.exists(task.export_file):
                os.remove(task.export_file)
            queue._finish(task)
            return
        try:
            with open(task.export_file, "r", encoding="utf-8") as f:
                total = len(json.load(f).get("messages", []))
        except Exception:
            total = 1

    task.dl_dir = f"{DL_BASE_PATH}/{task.channel_id if task.kind == 'single' else task.source_id}"
    os.makedirs(task.dl_dir, exist_ok=True)
    try:
        task.before_files = set(os.listdir(task.dl_dir))
    except Exception:
        task.before_files = set()

    bot.edit_message_text(f"📥 *{label}*\n\n📡 Step 1/2: ✅\n⬇️ Step 2/2: 下载文件中...",
                          task.chat_id, step_msg.message_id, parse_mode="HTML")

    dl_argv = ["tdl", "-n", task.tdl_name, "dl", "-f", task.export_file, "-t", "16",
               "--pool", "0", "-d", task.dl_dir, "--rewrite-ext", "--reconnect-timeout", "0"]
    ext = get_user_ext(task.chat_id)
    if ext:
        dl_argv += ["-i", ext]
    dl_argv += ["--template", "{{ .FileName }}"]
    if TDL_PROXY:
        dl_argv += ["--proxy", TDL_PROXY]
    task.process = subprocess.Popen(dl_argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)

    last_size = 0
    last_time = time.time()
    while task.process.poll() is None and not task.canceled:
        time.sleep(3)
        tmp_count, _, done = _get_dl_progress(task.dl_dir)
        try:
            cur_size = 0
            for f in os.listdir(task.dl_dir):
                if f.endswith(".tmp"):
                    cur_size += os.path.getsize(os.path.join(task.dl_dir, f))
        except Exception:
            cur_size = 0
        now = time.time()
        elapsed = now - last_time
        speed_str = _format_size((cur_size - last_size) / elapsed) + "/s" if elapsed > 0 and cur_size > last_size else "..."
        last_size = cur_size
        last_time = now
        try:
            if tmp_count > 0:
                tail = f"📦 {_format_size(cur_size)}  ⚡ {speed_str}"
                if task.kind == "multi":
                    tail += f"  ✅ {done}/{total}"
                text = f"📥 *{label}*\n\n📡 Step 1/2: ✅\n⬇️ Step 2/2: 下载文件中...\n\n{tail}"
            else:
                text = f"📥 *{label}*\n\n📡 Step 1/2: ✅\n⬇️ Step 2/2: 等待连接..."
            bot.edit_message_text(text, task.chat_id, step_msg.message_id, parse_mode="HTML")
        except Exception:
            pass

    if task.kind == "multi" and os.path.exists(task.export_file):
        os.remove(task.export_file)

    if task.canceled:
        _cleanup_tmp(task.dl_dir)
        queue._finish(task)
        return

    if task.process.returncode == 0:
        if task.kind == "single":
            renamed = _rename_new_files(task.dl_dir, task.before_files, task.rename_name)
            try:
                if renamed is not None:
                    names = renamed
                else:
                    after_files = set(os.listdir(task.dl_dir))
                    names = sorted(after_files - set(task.before_files or []))
                    names = [f for f in names if not f.startswith('.') and not f.endswith('.tmp')]
                file_list, total_size = _format_file_list(task.dl_dir, names)
            except Exception:
                file_list, total_size = "无法列出文件", 0
            bot.send_message(task.chat_id, f"""✅ 单文件下载完成！
📥 链接：{task.link}
📁 保存目录：{task.dl_dir}/
🔑 使用TDL账号：@{task.tdl_name}
📦 下载大小：{_format_size(total_size)}
📂 下载文件：
{file_list}""")
        else:
            try:
                files = os.listdir(task.dl_dir)
                file_list = "\n".join(files[:20]) if files else "（无可列出的文件）"
                if len(files) > 20:
                    file_list += f"\n... 还有 {len(files) - 20} 个文件"
            except Exception:
                file_list = "无法列出文件"
            bot.send_message(task.chat_id, f"""✅ 批量下载完成！
📥 源链接：{task.source_link} → 源频道ID：{task.source_id}
🆔 消息ID范围：{task.start_id}-{task.end_id}
📁 保存目录：{task.dl_dir}/
🔑 使用TDL账号：@{task.tdl_name}
📂 下载文件：
{file_list}""")
    else:
        _cleanup_tmp(task.dl_dir)
        bot.send_message(task.chat_id, f"❌ 下载失败！\n🔑 使用TDL账号：@{task.tdl_name}")
    queue._finish(task)


def do_single_download(chat_id, channel_id, msg_id, link, rename_name=None):
    """把单文件下载任务提交到队列。"""
    cid_str = str(chat_id)
    tdl_name = user_current_tdl.get(cid_str)
    user_accounts = TDL_ACCOUNTS.get(cid_str, [])
    if not tdl_name and user_accounts:
        tdl_name = user_accounts[0]
    if not tdl_name:
        bot.send_message(chat_id, "❌ 严重错误：未找到您的专属 TDL 账号！")
        user_steps.pop(chat_id, None)
        main_menu(chat_id)
        return
    task = DownloadTask(task_id=0, chat_id=chat_id, tdl_name=tdl_name, kind="single",
                        channel_id=channel_id, msg_id=msg_id, link=link, rename_name=rename_name)
    queue.submit(task)
    bot.send_message(chat_id, f"✅ 已加入下载队列\n{queue.status_text(chat_id)}")
    user_steps.pop(chat_id, None)


def do_multi_download(chat_id, source_id, source_link, start_id, end_id):
    """把批量下载任务提交到队列。"""
    cid_str = str(chat_id)
    tdl_name = user_current_tdl.get(cid_str)
    user_accounts = TDL_ACCOUNTS.get(cid_str, [])
    if not tdl_name and user_accounts:
        tdl_name = user_accounts[0]
    if not tdl_name:
        bot.send_message(chat_id, "❌ 严重错误：未找到您的专属 TDL 账号！")
        user_steps.pop(chat_id, None)
        main_menu(chat_id)
        return
    task = DownloadTask(task_id=0, chat_id=chat_id, tdl_name=tdl_name, kind="multi",
                        source_id=source_id, source_link=source_link, start_id=start_id, end_id=end_id)
    queue.submit(task)
    bot.send_message(chat_id, f"✅ 已加入下载队列\n{queue.status_text(chat_id)}")
    user_steps.pop(chat_id, None)


queue = DownloadQueue(MAX_CONCURRENT_DL, _run_task)
```

### 3.6 语法检查 + Commit

Run: `cd "D:/Users/siila/Desktop/hub上传项目/tdl-downloader" && python -m py_compile tdl.py`
Expected: 无输出（通过）

```bash
git add tdl.py
git commit -m "refactor: 下载执行改为任务队列 worker（_run_task）"
```

---

## Task 4: 改 `_kill_stale_tdl` / `main_menu` / `cmd_cancel`

**Files:**
- Modify: `tdl.py`

- [ ] **Step 1: `_kill_stale_tdl` 改用 `queue.running_pids()`**

在 `_kill_stale_tdl` 里，把这段（当前约 179-184 行）：

```python
    for proc in list(active_downloads.values()):
        try:
            if proc.poll() is None:
                protected.add(proc.pid)
        except Exception:
            pass
```

替换为：

```python
    protected |= queue.running_pids()
```

- [ ] **Step 2: `main_menu` 不再杀下载**

删除 `main_menu` 里这 3 行（当前约 347-349 行）：

```python
    if chat_id in active_downloads:
        active_downloads[chat_id].kill()
        del active_downloads[chat_id]
```

- [ ] **Step 3: `cmd_cancel` 改用 `queue.cancel_all`**

把 `cmd_cancel` 整个函数体替换为：

```python
@bot.message_handler(commands=['cancel'])
def cmd_cancel(msg):
    """取消当前操作：该用户全部下载任务 + 登录/任何进行中的输入"""
    chat_id = msg.chat.id
    n = queue.cancel_all(chat_id)
    if chat_id in active_logins:
        active_logins[chat_id].close(force=True)
        del active_logins[chat_id]
    user_steps.pop(chat_id, None)
    bot.send_message(chat_id, f"❌ 已取消 {n} 个下载任务")
    main_menu(chat_id)
```

- [ ] **Step 4: 语法检查 + Commit**

Run: `cd "D:/Users/siila/Desktop/hub上传项目/tdl-downloader" && python -m py_compile tdl.py`
Expected: 无输出（通过）

```bash
git add tdl.py
git commit -m "refactor: main_menu 不再杀下载，取消改为 queue.cancel_all"
```

---

## Task 5: `handle_steps` 抽取批量下载 + 删除 DOWNLOADING 步骤

**Files:**
- Modify: `tdl.py`

- [ ] **Step 1: `MULTI_DL_END_LINK` 分支改为调用 `do_multi_download`**

在 `handle_steps` 的 `elif step == "MULTI_DL_END_LINK":` 分支里，**保留** `end_link` 解析与两个校验 `if`（频道不一致、格式无效），把从 `source_id = data["source_id"]` 开始到 `t.start()` 结束的整段（当前约 1161-1225 行，含 `dl_dir`/`export_file`/`os.makedirs`、`tdl_name` 校验、`_kill_stale_tdl`、export、`check_export_file`、dl Popen、`t = threading.Thread(...)` 等）**全部删除**，替换为一行：

```python
        do_multi_download(chat_id, data["source_id"], data["source_link"], data["start_id"], end_msg_id)
```

替换后该分支为：

```python
    elif step == "MULTI_DL_END_LINK":
        end_link = msg.text.strip()
        end_channel_id, end_msg_id = extract_channel_and_msg_id_from_link(end_link)
        if not end_channel_id or not end_msg_id:
            bot.send_message(chat_id, "❌ 链接格式无效！请输入包含消息ID的完整链接")
            del user_steps[chat_id]
            main_menu(chat_id)
            return
        if end_channel_id != data["source_id"]:
            bot.send_message(chat_id, f"❌ 结束链接的频道ID（{end_channel_id}）与起始频道ID（{data['source_id']}）不一致！")
            del user_steps[chat_id]
            main_menu(chat_id)
            return
        do_multi_download(chat_id, data["source_id"], data["source_link"], data["start_id"], end_msg_id)
```

- [ ] **Step 2: 删除 `DOWNLOADING` 步骤分支**

删除 `handle_steps` 末尾的这段（当前约 1260-1262 行）：

```python
    # ================= 下载中（等待完成或取消） =================
    elif step == "DOWNLOADING":
        bot.send_message(chat_id, "⏳ 下载正在进行中... 发送 /cancel 或点击 ❌ 取消 可中止")
```

- [ ] **Step 3: 语法检查 + Commit**

Run: `cd "D:/Users/siila/Desktop/hub上传项目/tdl-downloader" && python -m py_compile tdl.py`
Expected: 无输出（通过）

```bash
git add tdl.py
git commit -m "refactor: 批量下载接入队列，移除 DOWNLOADING 步骤"
```

---

## Task 6: 配置与文档

**Files:**
- Modify: `docker-compose.yml`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: `docker-compose.yml` 加 `MAX_CONCURRENT_DL`**

在 `environment:` 段 `DL_BASE_PATH: /downloads` 之后加一行：

```yaml
      MAX_CONCURRENT_DL: "2"        # 同时下载的最大任务数
```

- [ ] **Step 2: `README.md` 功能列表补一行**

在「## 功能」列表末尾加：

```markdown
- **⚡ 并发下载队列** — 支持同时提交多个下载任务，全局并发上限 + 排队，同账号串行
```

- [ ] **Step 3: `CHANGELOG.md` 顶部加 v1.2.0**

在 `# 更新日志 (Changelog)` 的「记录格式」代码块之后、`## [v1.1.0]` 之前插入：

```markdown
## [v1.2.0] - 2026-08-18

### 新增
- 并发下载队列：支持同时提交多个下载任务，全局并发上限（`MAX_CONCURRENT_DL`，默认 2）+ 排队
- 同一 tdl 账号强制串行，不同账号可并行
- `/cancel` 取消本人全部下载任务（排队 + 进行中）

### 变更
- 返回主菜单不再中止正在进行的下载
```

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml README.md CHANGELOG.md
git commit -m "docs: 并发队列配置与文档"
```

---

## Task 7: 手动集成验证

> 需要真实 tdl 账号与服务器环境，无法在本地自动化。

- [ ] **Step 1: 运行单测，确认无回归**

Run: `cd "D:/Users/siila/Desktop/hub上传项目/tdl-downloader" && python -m pytest tests/test_download_queue.py -v`
Expected: 3 passed

- [ ] **Step 2: 服务器部署后手动验证**

1. `docker compose pull && docker compose up -d`
2. 机器人内连发 3 个 `https://t.me/...` 链接
3. 预期：前 2 个开始下载、第 3 个提示「排队 1 个 · 正在下载 2 个」；第 1 个完成后第 3 个自动开始
4. 发 `/cancel`，预期提示「已取消 N 个下载任务」并全部中止
5. 用同一个账号连发 2 个链接，预期严格串行（不会并发撞锁）

---

## 自审记录

- **Spec 覆盖**：全局上限+排队（Task 2/3）、同账号串行（Task 2 `busy_accounts`）、取消全部（Task 2 `cancel_all` + Task 4）、task_id 独立追踪（Task 2/3）、`main_menu` 不杀下载（Task 4）、`_kill_stale_tdl` 收窄（Task 4）、合并 watch（Task 3 `_run_task`）、`MAX_CONCURRENT_DL` 配置（Task 3.2 + Task 6）、单测（Task 2）。
- **类型一致性**：`DownloadTask` 字段、`DownloadQueue` 方法名（`submit`/`_finish`/`cancel_all`/`running_pids`/`counts`/`status_text`）在 `download_queue.py` 与 `tdl.py` 引用处一致。
- **无占位符**：所有步骤含完整代码与命令。
