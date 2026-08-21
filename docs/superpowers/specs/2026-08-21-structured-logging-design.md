# 结构化日志改造设计

日期：2026-08-21

## 背景

当前 `tdl.py` 只有 11 处 `print()`，且全是很粗的：启动两条、保存失败几条、连接重试一条。**下载流程、登录、队列、账号切换等关键动作完全没有日志**，事件只通过机器人消息发给用户，服务端 `docker compose logs` 里什么都看不到。排查问题（如之前的「任务卡住无法取消」）只能靠用户截图转发。

目标：用 Python 标准库 `logging` 替换全部 `print`，输出与用户另一个项目（TgtoDrive）一致的格式：

```
2026-08-21 12:14:53.918 - __main__ - INFO - 机器人启动，配置加载完成
```

## 方案（已选定 A）

直接在 `tdl.py` 顶部用 `logging.basicConfig` 配置一次格式，各组件用命名 logger。零新增依赖、零新文件。

## 日志配置

- 格式：`"%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - %(message)s"`
- 时间：`datefmt="%Y-%m-%d %H:%M:%S"`，配合 `%(msecs)03d` 得到 `.918` 毫秒后缀
- 级别：默认 `INFO`，可用环境变量 `LOG_LEVEL` 覆盖（`DEBUG` / `INFO` / `WARNING`）
- 位置：`tdl.py` 最顶部，在环境变量校验**之前**，保证校验失败时也能 `logger.critical` 留下记录再 `exit(1)`
- telebot 自身的 logger（`telebot`）设为 `WARNING`，避免其内部 INFO 刷屏（可选，若觉得需要可保留 INFO）

## 命名 logger

| logger | 职责 |
|--------|------|
| `__main__` | 启动、配置加载、菜单、连接重试 |
| `queue` | 队列状态变化 |
| `download` | 下载任务生命周期、tdl 子进程、孤儿进程清理 |
| `login` | 账号登录/绑定/切换/删除 |

## 日志点清单

| 组件 | 级别 | 日志点 |
|------|------|--------|
| `__main__` | INFO | 配置加载完成（`MAX_CONCURRENT_DL`、`DL_BASE_PATH`、`TDL_PROXY` 是否启用） |
| `__main__` | INFO | 持久化数据加载完成（账号 N 个、管理员 N 个） |
| `__main__` | INFO | 左下角菜单设置完成（6 项） |
| `__main__` | INFO | 机器人已启动，等待指令 |
| `__main__` | WARNING | 连接异常，5 秒后重试（带异常） |
| `__main__` | CRITICAL | 缺 `DL_BASE_PATH` / `BOT_TOKEN` / `SUPER_ADMIN` 非法（记录后 `exit(1)`） |
| `queue` | INFO | 任务入队（task_id、kind、账号，带 `排队X·运行Y`） |
| `queue` | INFO | 任务开始运行 |
| `queue` | INFO | 任务收尾/释放 |
| `queue` | INFO | 取消任务（数量） |
| `download` | INFO | 收到任务（单文件/批量、账号、链接或 ID 范围） |
| `download` | INFO | Step1 导出 开始 / 成功 / 失败 |
| `download` | INFO | Step2 下载 开始 / 完成（大小+耗时）/ 失败 |
| `download` | ERROR | 任务异常终止（带异常堆栈） |
| `download` | DEBUG | 每 3 秒进度轮询（只入 DEBUG，不刷 INFO） |
| `download` | INFO | 孤儿 tdl 进程清理（清理了几个） |
| `login` | INFO | 登录开始（手机号脱敏）/ 验证码已发 / 2FA 提交 / 成功 / 失败 |
| `login` | INFO | 绑定 / 切换 / 删除账号 |
| 各 `except` | ERROR | 关键路径补 `logger.error(..., exc_info=True)` |

## 敏感信息红线

以下内容**一律不进日志**：

- `BOT_TOKEN`
- 手机号（脱敏成 `138****1234`）
- 验证码
- 2FA 密码

登录交互（pexpect）里出现的任何验证码/密码，日志里只记「已发送 / 已提交」，不记内容。

## 错误处理

- `save_*` 系列保存失败：`print` → `logger.error(..., exc_info=True)`
- 下载任务异常：`logger.exception`（`_run_task` 的 `except` 分支）
- `run_command` 超时/非零退出：`logger.warning` / `logger.error`（带退出码或错误摘要）

## 测试与验收

- `python -m py_compile tdl.py` 通过
- 现有 `tests/test_download_queue.py` 4 个测试保持通过
- 日志本身靠 `docker compose logs -f tdl-downloader` 手动验收：启动、入队、导出、下载、完成、取消各有一条 INFO，且无敏感信息泄漏

## 范围之外（YAGNI）

- 不引入第三方日志库
- 不做日志文件落盘（Docker 已用 `json-file` + `max-size`/`max-file` 轮转）
- 不做结构化 JSON 日志（保持与 TgtoDrive 一致的纯文本格式）
- 不改动 `download_queue.py` 的核心逻辑（仅在 `tdl.py` 侧打日志；若需在 queue 内部打日志，通过回调或只在 `tdl.py` 收尾处记录）
