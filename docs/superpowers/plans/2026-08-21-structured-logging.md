# 结构化日志改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用标准库 `logging` 替换 `tdl.py` 里的全部 `print`，并补齐下载/队列/登录等关键动作的日志，输出格式与 TgtoDrive 一致（`时间.毫秒 - 模块 - 级别 - 消息`）。

**Architecture:** 新增一个可独立单测的小模块 `logutil.py`（`setup_logging` + `redact_phone`），`tdl.py` 顶部导入并调用一次 `setup_logging`，各组件用命名 logger（`__main__` / `queue` / `download` / `login`）打点。不改动 `download_queue.py` 核心逻辑。

**Tech Stack:** Python 标准库 `logging`；`pytest` 做单元测试。

---

## File Structure

- **Create** `logutil.py` — 日志配置常量 + `setup_logging()` + `redact_phone()`（可独立导入测试）
- **Create** `tests/test_logutil.py` — 单测脱敏与格式
- **Modify** `tdl.py` — 顶部导入并配置日志；替换全部 `print`；补齐下载/队列/登录日志点
- **Modify** `docker-compose.yml` — 加一行可选的 `LOG_LEVEL` 注释说明（不改运行行为）

测试运行方式：在项目根目录用 `python -m pytest`（这样 `download_queue` / `logutil` 都在 `sys.path` 上）。

---

## Task 1: 新建 logutil.py（TDD）

**Files:**
- Create: `logutil.py`
- Test: `tests/test_logutil.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_logutil.py`：

```python
import logging
import re

from logutil import LOG_FORMAT, LOG_DATEFMT, redact_phone


def test_redact_phone_standard():
    assert redact_phone("13812345678") == "138****5678"


def test_redact_phone_with_country_code():
    assert redact_phone("+8613812345678") == "861****5678"


def test_redact_phone_too_short_untouched():
    assert redact_phone("123456") == "123456"


def test_redact_phone_non_digit_untouched():
    assert redact_phone("abc") == "abc"


def test_log_format_milliseconds_and_parts():
    fmt = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    record = logging.LogRecord("__main__", logging.INFO, "tdl.py", 1, "机器人启动", None, None)
    out = fmt.format(record)
    parts = out.split(" - ")
    assert parts[1:] == ["__main__", "INFO", "机器人启动"]
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$", parts[0])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_logutil.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'logutil'`）

- [ ] **Step 3: 写 logutil.py**

```python
"""结构化日志配置与手机号脱敏工具。

独立成模块：导入它不会触发 tdl.py 的环境变量校验 / exit，方便单元测试。
"""
import logging

LOG_FORMAT = "%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level=None):
    """配置根 logger，输出格式：
    2026-08-21 12:14:53.918 - __main__ - INFO - 消息
    """
    level_name = (level or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format=LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        force=True,
    )
    # telebot 内部 INFO 太吵，压到 WARNING
    logging.getLogger("telebot").setLevel(logging.WARNING)


def redact_phone(phone):
    """手机号脱敏：13812345678 -> 138****5678。长度不足或非数字原样返回。"""
    digits = str(phone).lstrip("+").replace(" ", "")
    if len(digits) >= 7 and digits.isdigit():
        return digits[:3] + "****" + digits[-4:]
    return str(phone)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_logutil.py -v`
Expected: PASS（5 个测试全过）

- [ ] **Step 5: 提交**

```bash
git add logutil.py tests/test_logutil.py
git commit -m "feat: 新增 logutil（日志配置 + 手机号脱敏）"
```

---

## Task 2: tdl.py 顶部配置 + 启动/校验/保存日志

**Files:**
- Modify: `tdl.py`（顶部 1-32 行、`load_data` 调用处 ~156 行、`save_*` 系列、`set_bot_commands`、`__main__` 块）
- Modify: `docker-compose.yml`（可选注释）

- [ ] **Step 1: 替换 tdl.py 顶部 import 与环境变量校验**

把 `tdl.py` 顶部（当前第 1-32 行）替换为：

```python
import re
import json
import subprocess
import os
import threading
import time
import logging
import telebot
import pexpect
from time import sleep
from functools import wraps
from telebot.types import BotCommand
from download_queue import DownloadTask, DownloadQueue
from logutil import setup_logging, redact_phone

# ---- 日志：先配置，保证后面的配置校验失败也能留下记录 ----
setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("__main__")
queue_log = logging.getLogger("queue")
download_log = logging.getLogger("download")
login_log = logging.getLogger("login")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SUPER_ADMIN_RAW = os.environ.get("SUPER_ADMIN", "")
DL_BASE_PATH = os.environ.get("DL_BASE_PATH", "").rstrip("/")
if not DL_BASE_PATH:
    log.critical("未设置 DL_BASE_PATH 环境变量！请在 docker-compose.yml 中配置下载目录。")
    exit(1)
TDL_PROXY = os.environ.get("TDL_PROXY", "")  # 代理，如 socks5://192.168.31.2:7891
MAX_CONCURRENT_DL = int(os.environ.get("MAX_CONCURRENT_DL", "2") or 2)

if not BOT_TOKEN or not SUPER_ADMIN_RAW:
    log.critical("未设置 BOT_TOKEN 或 SUPER_ADMIN 环境变量！")
    log.critical("   示例: BOT_TOKEN=123456:ABCdef... SUPER_ADMIN=987654321 docker compose up -d")
    exit(1)

try:
    SUPER_ADMIN = int(SUPER_ADMIN_RAW)
except ValueError:
    log.critical("SUPER_ADMIN 必须是纯数字！当前值: %s", SUPER_ADMIN_RAW)
    exit(1)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

log.info("配置加载完成：MAX_CONCURRENT_DL=%d, DL_BASE_PATH=%s, TDL_PROXY=%s",
         MAX_CONCURRENT_DL, DL_BASE_PATH, "已启用" if TDL_PROXY else "未启用")
```

- [ ] **Step 2: `load_data()` 后加数据加载日志**

找到 `# 初始化加载数据` 那行的 `load_data()`（当前第 156 行），改为：

```python
# 初始化加载数据
load_data()
log.info("持久化数据加载完成：TDL账号 %d 个、管理员 %d 个",
         sum(len(v) for v in TDL_ACCOUNTS.values()), len(ADMIN_LIST))
```

- [ ] **Step 3: 替换 `save_*` 系列的 print**

`save_admins` / `save_tdl_accounts` / `save_user_current_tdl` / `save_user_dl_ext` 四个函数里的 `print(...)` 全部改为 `log.error(..., exc_info=True)`。以 `save_admins` 为例：

```python
def save_admins():
    """保存管理员列表到文件"""
    try:
        with open(ADMIN_FILE, "w", encoding="utf-8") as f:
            json.dump(ADMIN_LIST, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("保存管理员列表失败：%s", e, exc_info=True)
```

其余三个同理，分别替换为：

- `save_tdl_accounts`：`log.error("保存TDL账号失败：%s", e, exc_info=True)`
- `save_user_current_tdl`：`log.error("保存用户TDL账号失败：%s", e, exc_info=True)`
- `save_user_dl_ext`：`log.error("保存下载类型失败：%s", e, exc_info=True)`

- [ ] **Step 4: `set_bot_commands` 的 print 改 log**

把 `set_bot_commands` 末尾（当前第 171 行）：

```python
    bot.set_my_commands(commands)
    print("✅ 左下角固定菜单设置完成")
```

改为：

```python
    bot.set_my_commands(commands)
    log.info("左下角菜单设置完成，共 %d 项", len(commands))
```

- [ ] **Step 5: `__main__` 块的启动/重试日志**

把文件末尾的 `if __name__ == "__main__":` 块（当前第 1232-1240 行）改为：

```python
if __name__ == "__main__":
    set_bot_commands()
    log.info("机器人已启动，等待指令")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            log.warning("连接异常，5秒后重试: %s", e)
            sleep(5)
```

- [ ] **Step 6: docker-compose.yml 加可选 LOG_LEVEL 注释**

在 `MAX_CONCURRENT_DL: "2"` 那行下面加一行注释（不改变运行行为）：

```yaml
      MAX_CONCURRENT_DL: "2"        # 跨账号的最大并发下载数（同账号始终串行）
      # LOG_LEVEL: "INFO"           # 可选：日志级别 DEBUG/INFO/WARNING，默认 INFO
```

- [ ] **Step 7: 编译检查 + 提交**

Run: `python -m py_compile tdl.py logutil.py`
Expected: 无输出（通过）

```bash
git add tdl.py docker-compose.yml
git commit -m "feat: 接入结构化日志（配置/启动/校验/保存）"
```

---

## Task 3: queue + download 日志

**Files:**
- Modify: `tdl.py`（`do_single_download`、`do_multi_download`、`_run_task`、`_run_task_body`、`run_command`、`_kill_stale_tdl`、`cmd_cancel`）

- [ ] **Step 1: `do_single_download` 入队日志**

把 `do_single_download` 里（当前第 813-817 行）：

```python
    task = DownloadTask(task_id=0, chat_id=chat_id, tdl_name=tdl_name, kind="single",
                        channel_id=channel_id, msg_id=msg_id, link=link, rename_name=rename_name)
    queue.submit(task)
    bot.send_message(chat_id, f"✅ 已加入下载队列\n{queue.status_text(chat_id)}")
    user_steps.pop(chat_id, None)
```

改为：

```python
    task = DownloadTask(task_id=0, chat_id=chat_id, tdl_name=tdl_name, kind="single",
                        channel_id=channel_id, msg_id=msg_id, link=link, rename_name=rename_name)
    queue.submit(task)
    download_log.info("[任务#%d] 收到单文件下载（账号 @%s，链接 %s）", task.task_id, tdl_name, link)
    queue_log.info("[任务#%d] 已入队 %s", task.task_id, queue.status_text(chat_id))
    bot.send_message(chat_id, f"✅ 已加入下载队列\n{queue.status_text(chat_id)}")
    user_steps.pop(chat_id, None)
```

- [ ] **Step 2: `do_multi_download` 入队日志**

把 `do_multi_download` 里（当前第 832-836 行）：

```python
    task = DownloadTask(task_id=0, chat_id=chat_id, tdl_name=tdl_name, kind="multi",
                        source_id=source_id, source_link=source_link, start_id=start_id, end_id=end_id)
    queue.submit(task)
    bot.send_message(chat_id, f"✅ 已加入下载队列\n{queue.status_text(chat_id)}")
    user_steps.pop(chat_id, None)
```

改为：

```python
    task = DownloadTask(task_id=0, chat_id=chat_id, tdl_name=tdl_name, kind="multi",
                        source_id=source_id, source_link=source_link, start_id=start_id, end_id=end_id)
    queue.submit(task)
    download_log.info("[任务#%d] 收到批量下载（账号 @%s，%s[%s-%s]）",
                      task.task_id, tdl_name, source_id, start_id, end_id)
    queue_log.info("[任务#%d] 已入队 %s", task.task_id, queue.status_text(chat_id))
    bot.send_message(chat_id, f"✅ 已加入下载队列\n{queue.status_text(chat_id)}")
    user_steps.pop(chat_id, None)
```

- [ ] **Step 3: `_run_task` 开始/异常/收尾日志**

把 `_run_task`（当前第 642-658 行）改为：

```python
def _run_task(task):
    """在独立线程中执行一个下载任务：export → dl → 轮询进度 → 收尾。
    任何异常都走 finally 收尾，保证任务不会卡在 running 里无法取消。"""
    queue_log.info("[任务#%d] 开始运行 %s", task.task_id, task.kind)
    try:
        _run_task_body(task)
    except Exception as e:
        download_log.exception("[任务#%d] 异常终止", task.task_id)
        try:
            bot.send_message(task.chat_id, f"❌ 任务异常终止：{str(e)[:200]}")
        except Exception:
            pass
    finally:
        try:
            if task.process is not None and task.process.poll() is None:
                task.process.kill()
        except Exception:
            pass
        queue._finish(task)
        queue_log.info("[任务#%d] 收尾 %s", task.task_id, queue.status_text(task.chat_id))
```

- [ ] **Step 4: `_run_task_body` 导出阶段日志**

在 `_run_task_body` 里 `_kill_stale_tdl()`（当前第 665 行）之后加一行：

```python
    _kill_stale_tdl()
    download_log.info("[任务#%d] Step1 导出消息（账号 @%s）", task.task_id, task.tdl_name)
```

把导出结果判断（当前第 681-687 行）：

```python
    success, _, stderr, _ = run_command(export_argv, task.chat_id, task)

    if task.canceled:
        return
    if not success:
        bot.edit_message_text(f"❌ 导出失败：{stderr[:200]}", task.chat_id, step_msg.message_id)
        return
```

改为：

```python
    success, _, stderr, _ = run_command(export_argv, task.chat_id, task)

    if task.canceled:
        download_log.info("[任务#%d] 导出阶段被取消", task.task_id)
        return
    if not success:
        download_log.error("[任务#%d] 导出失败: %s", task.task_id, stderr[:200])
        bot.edit_message_text(f"❌ 导出失败：{stderr[:200]}", task.chat_id, step_msg.message_id)
        return
    download_log.info("[任务#%d] Step1 导出成功", task.task_id)
```

- [ ] **Step 5: `_run_task_body` 下载阶段日志**

在下载启动前（当前第 720-722 行）：

```python
    if task.canceled:
        return
    task.process = subprocess.Popen(dl_argv, ...)
```

改为：

```python
    if task.canceled:
        return
    download_log.info("[任务#%d] Step2 下载中（共 %d 条）", task.task_id, total)
    task.process = subprocess.Popen(dl_argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
```

在轮询循环里 `last_time = now`（当前第 740 行）之后加一行 DEBUG 心跳：

```python
        last_time = now
        download_log.debug("[任务#%d] 进度: %s 临时%d个", task.task_id, speed_str, tmp_count)
```

把下载阶段取消分支（当前第 757-759 行）：

```python
    if task.canceled:
        _cleanup_tmp(task.dl_dir)
        return
```

改为：

```python
    if task.canceled:
        _cleanup_tmp(task.dl_dir)
        download_log.info("[任务#%d] 下载阶段被取消", task.task_id)
        return
```

- [ ] **Step 6: `_run_task_body` 完成/失败日志**

单文件完成分支（当前第 761-764 行附近），在 `file_list, total_size = _format_file_list(task.dl_dir, names)` 之后加：

```python
            file_list, total_size = _format_file_list(task.dl_dir, names)
            download_log.info("[任务#%d] 单文件下载完成 %s", task.task_id, _format_size(total_size))
```

批量完成分支，在 `files = os.listdir(task.dl_dir)` 之后、`file_list = "\n".join(...)` 之前加：

```python
                files = os.listdir(task.dl_dir)
                download_log.info("[任务#%d] 批量下载完成（%d 个文件）", task.task_id, len(files))
```

失败分支（当前第 796-798 行）改为：

```python
    else:
        _cleanup_tmp(task.dl_dir)
        download_log.error("[任务#%d] 下载失败（退出码 %s）", task.task_id, task.process.returncode)
        bot.send_message(task.chat_id, f"❌ 下载失败！\n🔑 使用TDL账号：@{task.tdl_name}")
```

- [ ] **Step 7: `run_command` 超时/退出码日志**

把 `run_command` 里超时分支（当前第 240-243 行）：

```python
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return False, "", "操作超时（5分钟），请减少消息数量重试", tdl_name
```

改为：

```python
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            download_log.warning("tdl 命令超时（300s）：%s", " ".join(argv))
            return False, "", "操作超时（5分钟），请减少消息数量重试", tdl_name
```

在 `return proc.returncode == 0, stdout, error_msg, tdl_name`（当前第 248 行）之前加一行非零退出 DEBUG：

```python
        if proc.returncode != 0:
            download_log.debug("tdl 命令退出码 %s：%s", proc.returncode, " ".join(argv))
        return proc.returncode == 0, stdout, error_msg, tdl_name
```

- [ ] **Step 8: `_kill_stale_tdl` 清理计数日志**

把 `_kill_stale_tdl` 的 `try` 块（当前第 188-203 行）改为带计数器并在结尾打日志：

```python
    killed = 0
    try:
        for p in os.listdir("/proc"):
            if not p.isdigit():
                continue
            pid = int(p)
            if pid in protected:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read()
                if b"tdl" in cmdline:
                    os.kill(pid, 9)
                    killed += 1
            except Exception:
                pass
    except Exception:
        pass
    if killed:
        download_log.info("清理孤儿 tdl 进程 %d 个", killed)
```

（`protected` 的构造代码保持原样，只改 `try` 块。）

- [ ] **Step 9: `cmd_cancel` 取消日志**

在 `cmd_cancel` 里 `n = queue.cancel_all(chat_id)`（当前第 880 行）之后加：

```python
    n = queue.cancel_all(chat_id)
    queue_log.info("用户 %s 取消 %d 个任务", chat_id, n)
```

- [ ] **Step 10: 编译 + 回归测试 + 提交**

Run:
```
python -m py_compile tdl.py
python -m pytest -v
```
Expected: `py_compile` 无输出；pytest 全部通过（含 Task 1 的 5 个 + 原有 4 个队列测试）

```bash
git add tdl.py
git commit -m "feat: 下载/队列流程结构化日志"
```

---

## Task 4: login 日志

**Files:**
- Modify: `tdl.py`（`handle_steps` 登录各分支、`switch_tdl_account`）

- [ ] **Step 1: LOGIN_PHONE 开始/发送日志**

在 `handle_steps` 的 `LOGIN_PHONE` 分支里，`tdl_name = data["tdl_name"]`（当前第 957 行）之后加：

```python
        phone = msg.text.strip()
        tdl_name = data["tdl_name"]
        login_log.info("用户 %s 开始登录账号 @%s（手机号 %s）", chat_id, tdl_name, redact_phone(phone))
```

在 `data["step"] = "LOGIN_CODE"`（当前第 984 行）之前加：

```python
            login_log.info("账号 @%s 验证码已请求发送", tdl_name)
            data["step"] = "LOGIN_CODE"
```

超时分支（当前第 987-990 行）改为（**不把 `safe_out` 写进服务端日志**）：

```python
        except pexpect.TIMEOUT:
            login_log.warning("账号 @%s 登录请求超时", tdl_name)
            safe_out = str(child.before).replace('<', '[').replace('>', ']')
            bot.send_message(chat_id, f"❌ 请求超时！\n终端截获：\n{safe_out}")
            _cleanup_login(chat_id)
```

异常分支（当前第 991-994 行）改为：

```python
        except Exception as e:
            login_log.warning("账号 @%s 启动登录失败: %s", tdl_name, e)
            safe_error = str(e).replace('<', '[').replace('>', ']')
            bot.send_message(chat_id, f"❌ 启动登录失败：\n{safe_error}")
            _cleanup_login(chat_id)
```

- [ ] **Step 2: LOGIN_CODE 提交/成功日志**

在 `LOGIN_CODE` 分支里 `code = msg.text.strip()`（当前第 998 行）之后加（**不记录验证码内容**）：

```python
        code = msg.text.strip()
        login_log.info("账号 @%s 提交验证码", data.get("tdl_name"))
```

在登录成功分支 `_bind_tdl_account(chat_id, data["tdl_name"])`（当前第 1026 行）之前加：

```python
                    login_log.info("账号 @%s 登录成功", data["tdl_name"])
                    _bind_tdl_account(chat_id, data["tdl_name"])
```

- [ ] **Step 3: LOGIN_PASSWORD 提交/成功/失败日志**

在 `LOGIN_PASSWORD` 分支里 `password = msg.text.strip()`（当前第 1040 行）之后加（**不记录密码**）：

```python
        password = msg.text.strip()
        login_log.info("账号 @%s 提交2FA密码", data.get("tdl_name"))
```

在密码验证成功分支 `_bind_tdl_account(...)`（当前第 1054 行）之前加：

```python
                    login_log.info("账号 @%s 2FA验证通过，登录成功", data["tdl_name"])
                    _bind_tdl_account(chat_id, data["tdl_name"],
                                      f"✅ 两步验证通过！登录成功！账号 @{data['tdl_name']} 已绑定并设为您当前的专属账号。")
```

失败分支（当前第 1061-1063 行）改为：

```python
        except Exception:
            login_log.warning("账号 @%s 密码错误或登录失败", data.get("tdl_name"))
            bot.send_message(chat_id, "❌ 密码错误或登录失败。")
            _cleanup_login(chat_id)
```

- [ ] **Step 4: 绑定/删除/切换日志**

`ADD_TDL_NAME` 成功分支，在 `bot.send_message(chat_id, f"✅ 成功将 TDL 账号 @{name} 绑定...")`（当前第 914 行）之前加：

```python
            login_log.info("账号 @%s 绑定到用户 %s", name, chat_id)
```

`DEL_TDL_NAME` 成功分支，在 `bot.send_message(chat_id, f"✅ 已从您的名下删除...")`（当前第 933 行）之前加：

```python
            login_log.info("账号 @%s 从用户 %s 删除", name, chat_id)
```

`switch_tdl_account` 里 `save_user_current_tdl()`（当前第 1225 行）之后加：

```python
    user_current_tdl[cid_str] = name
    save_user_current_tdl()
    login_log.info("用户 %s 切换账号到 @%s", cid_str, name)
```

- [ ] **Step 5: 编译 + 回归测试 + 提交**

Run:
```
python -m py_compile tdl.py
python -m pytest -v
```
Expected: 全部通过

```bash
git add tdl.py
git commit -m "feat: 登录流程结构化日志（含手机号脱敏）"
```

---

## Task 5: 全量验证 + 收尾

- [ ] **Step 1: 全量编译 + 测试**

Run:
```
python -m py_compile tdl.py logutil.py
python -m pytest -v
```
Expected: 无编译错误；pytest 全部通过（原有 4 个 + 新增 5 个）

- [ ] **Step 2: 日志敏感信息自查**

Run: `python -m pytest tests/test_logutil.py -v -q`（已覆盖脱敏逻辑）

再人工核对 `tdl.py` 中 `login_log` 的调用没有把 `code` / `password` / `safe_out` 写进日志。

- [ ] **Step 3: 确认无遗漏 print**

Run: `grep -n "print(" tdl.py`
Expected: 无输出（`tdl.py` 内不再有 `print`）

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "chore: 结构化日志改造完成"
```

---

## 验收方式（部署后）

```bash
docker compose pull && docker compose up -d
docker compose logs -f tdl-downloader
```

预期看到：配置加载完成 → 数据加载完成 → 菜单设置完成 → 机器人已启动；提交下载时看到「收到任务 / 已入队 / 开始运行 / Step1 导出 / Step2 下载 / 完成 / 收尾」各一条 INFO，取消时看到取消日志，且无任何手机号明文/验证码/密码泄漏。

## 备注

- 版本号（`v1.3.0` 之类）与 CHANGELOG/RELEASE_NOTES 更新不在本计划内，按你之前的发版流程单独走。
