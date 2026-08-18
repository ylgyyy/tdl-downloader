# 并发下载队列设计

- 日期：2026-08-18
- 状态：已确认，待实现
- 范围：tdl-downloader 机器人（`tdl.py`）

## 背景与问题

当前下载执行模型存在并发缺陷：

- `active_downloads` 以 `chat_id` 为唯一 key（`tdl.py:46`），一个用户同时只能追踪一个下载。
- 同一用户连发多个下载时，第二个任务会覆盖第一个的进程引用；先完成的任务会误删追踪并触发 `main_menu` 杀进程，后完成的任务被误判为「已取消」，完成消息不发、`.tmp` 不清理。
- 「直接发链接下载」路径不经过 `user_steps`，导致同一用户能连发多个链接并踩进上述竞态。
- 无并发上限，多任务可打满机器；同一 tdl 账号并发下载会撞本地数据库锁（`database is locked`），现有代码靠 `_kill_stale_tdl()` 扫描 `/proc` 粗暴杀进程规避。

## 目标与非目标

**目标**
- 支持同一用户同时提交多个下载任务，按队列顺序执行。
- 全局并发上限（可配置）+ 排队。
- 同一 tdl 账号强制串行，不同账号可并行，彻底避开数据库锁。
- 每个任务独立追踪，完成/失败消息按任务发送。

**非目标（本次不做）**
- SQLite 持久化队列（重启恢复任务）。队列在内存中，重启丢失排队任务，接受。
- 断点续传、下载去重。
- 逐个任务选择取消（本次取消粒度 = 取消该用户全部任务）。

## 已确认的决策

| 决策点 | 选择 |
|--------|------|
| 并发模型 | 全局上限 + 排队 |
| 同账号并发 | 按账号串行 |
| `/cancel` 粒度 | 取消该用户全部任务（排队 + 进行中） |
| 实现方向 | 进程内任务队列 + 工作线程池（方案 A） |

## 组件与数据结构

### `DownloadTask`（dataclass）

```python
@dataclass
class DownloadTask:
    task_id: int              # 递增唯一 ID
    chat_id: int
    tdl_name: str             # 账号名，串行锁的 key
    kind: str                 # "single" | "multi"

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

    # 运行时状态（由队列填充）
    process: object = None    # subprocess.Popen，运行后设置
    export_file: str = None
    dl_dir: str = None
    step_msg_id: int = None   # 进度消息 id
    status: str = "queued"    # queued / running / done / failed / canceled
    before_files: set = None  # 下载前目录快照，用于重命名与大小统计
```

### `DownloadQueue`（线程安全类）

```python
class DownloadQueue:
    def __init__(self, max_concurrent: int)
    # 状态（均受 self.lock 保护）
    pending: deque           # FIFO
    running: dict            # task_id -> DownloadTask
    busy_accounts: set       # 正在运行的账号名
    _next_id: int

    def submit(self, task) -> int      # 入队并 _pump，返回 task_id
    def _pump(self)                    # 启动尽可能多的任务
    def _can_start(self, task) -> bool # len(running) < max 且 tdl_name 不在 busy_accounts
    def _start(self, task)             # 标记 running + busy，起线程 _run_task(task)
    def _run_task(self, task)          # export → dl Popen → 轮询进度 → 重命名/清理 → 完成消息 → _finish
    def _finish(self, task)            # 释放 running/busy，发送完成/失败消息，_pump
    def cancel_all(self, chat_id) -> int  # 取消该用户全部任务，返回取消数
    def running_pids(self) -> set      # 供 _kill_stale_tdl 保护
    def status_text(self, chat_id) -> str  # 「排队 N / 运行 M」反馈文案
```

## 数据流

**提交（单文件示例）**
1. `do_single_download(chat_id, channel_id, msg_id, link, rename_name)` 校验账号存在 → 构建 `DownloadTask` → `queue.submit(task)`。
2. `submit` 入队并 `_pump`；若无空位则仅入队。
3. 回复用户「✅ 已加入下载队列（排队 N 个 / 运行 M 个）」，清空该用户 `user_steps`。

**执行**
4. `_pump` 选中任务后 `_start`：起线程 `_run_task(task)`（每个运行中的任务一个线程，`export` 不再阻塞 handler 线程）。
5. `_run_task`：`export`（复用 `run_command`）→ `dl`（`subprocess.Popen`，参数与现状一致）→ 轮询进度（复用现有 `.tmp` 轮询）并编辑进度消息。
6. 完成/失败 → 重命名/清理（single 分支）→ `_finish(task)`：释放账号锁与运行位 → 发送完成消息（含 `📦 下载大小`）→ `_pump()` 拉下一个。

**取消**
7. `/cancel` → `queue.cancel_all(chat_id)`：kill 该用户所有 running 进程、移除其 pending 任务，回「已取消 N 个任务」。

## 语义变更

- `user_steps` 只管「输入向导」（登录、输链接、重命名），下载执行完全交给队列。删除 `step == "DOWNLOADING"` 及 `user_steps[...]["step"] = "DOWNLOADING"` 的相关逻辑。
- `main_menu` **不再杀下载**。返回主菜单只重置输入向导 + 显示菜单。取消唯一入口是 `/cancel`。
- `_watch_single_download` 与 `_watch_multi_download` 合并进 `_run_task(task)`（连同原 `do_single_download` 的 export+dl 逻辑），完成消息与重命名逻辑按 `task.kind` 分支。
- `_kill_stale_tdl()` 的 `protected` 集合改为 `queue.running_pids() + _running_tdl_pids + 登录进程 pid`；因同账号已串行，其职责退化为崩溃后的防御性清理。

## 配置

- 新增环境变量 `MAX_CONCURRENT_DL`（默认 `2`），`docker-compose.yml` 增加一行。
- Dockerfile 无需改动（纯 Python）。

## 错误处理

- 账号不存在：在 `do_single_download` 提交前校验并提示，不入队。
- export 失败：任务失败，发送错误、释放账号锁、`_pump` 下一任务。
- 下载失败：沿用现有清理（删除 `.tmp`）+ 错误消息。
- 队列状态并发访问：所有读写加 `threading.Lock`。

## 测试

- `DownloadQueue` 调度单测（纯 Python，不依赖 tdl）：全局上限生效、同账号串行顺序、`cancel_all` 只取消目标用户。
- `extract_channel_and_msg_id_from_link` 单测。
- 手动集成：真实 tdl 账号下连发多个链接，观察排队与并发。

## 风险

- `_kill_stale_tdl` 保留但范围收窄；若后续确认 tdl 进程退出会自释放锁，可评估移除整个 `/proc` 扫描。
- 队列在内存，进程重启丢失排队任务（进行中的下载进程也会被丢，tdl 无断点续传，损失有限）。
