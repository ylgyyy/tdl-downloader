# tdl-downloader

基于 [tdl](https://github.com/iyear/tdl) 的 Telegram 下载机器人。通过 Bot 界面输入消息链接，即可把 Telegram 频道 / 群组里的媒体文件下载到指定目录，无需在服务器上手动敲命令。

## 功能

- **📥 单文件下载** — 输入单条消息链接，精准下载该消息中的媒体
- **📥 批量下载** — 按消息 ID 范围批量下载
- **📎 下载类型过滤** — 图片 / 视频 / 文档 白名单，只下载需要的类型
- **✏️ 文件重命名** — 单文件下载时可自定义文件名
- **📊 实时进度** — 下载速度与文件大小实时刷新
- **🔐 多租户隔离** — 每个用户只能使用自己绑定的 TDL 账号
- **📱 机器人内登录** — 手机号 + 验证码 + 2FA，无需 SSH

## 快速开始

### 1. 准备工作

- Telegram 机器人 Token → 找 [@BotFather](https://t.me/BotFather)
- 你的 Telegram 数字 ID → 找 [@userinfobot](https://t.me/userinfobot)
- 一台装了 Docker 的服务器

### 2. 部署

编辑 `docker-compose.yml`，把其中的占位值改成真实值：

```yaml
environment:
  # ⚠️ 部署前把下面两个占位值改成真实值
  BOT_TOKEN: "123456:ABCdef..."   # 改成你的真实 Token
  SUPER_ADMIN: "987654321"        # 改成你的数字 ID
  DL_BASE_PATH: /downloads        # 容器内下载目录（保持不变）
```

再把下载目录的**宿主机路径**改成你的实际路径（冒号右边的容器内路径 `/downloads` 保持不变）：

```yaml
volumes:
  # NAS 下载目录（宿主机路径:容器内路径）
  - /vol4/1000/影视/tdl:/downloads
```

镜像由 GitHub Actions 自动构建并推送到 Docker Hub 公开仓库 `ylgy007/tdl-downloader`，直接拉取即可，**无需本地构建**：

```bash
docker compose up -d
```

### 3. 使用

部署完成后，在 Telegram 打开机器人发送 `/start`，进入主菜单。

## 使用说明

### 📥 单文件下载

下载单条消息中的媒体到本地目录。

```
1. 点击「单文件下载」（或发送 /single）
2. 输入原消息完整链接（如 https://t.me/c/123456/789）
3. 选择是否需要重命名：
   - 需要 → 输入新文件名（可带扩展名，如 我的电影.mp4）
   - 不需要 → 保留原文件名
4. 等待下载完成
```

### 📥 批量下载

按消息 ID 范围批量下载。

```
1. 点击「批量下载」（或发送 /multi）
2. 输入起始消息完整链接（如 https://t.me/c/123456/38359）
3. 输入结束消息完整链接（须同一频道）
4. 等待导出 + 下载完成
```

### 📎 下载类型过滤

只下载指定类型的文件，其余跳过。

```
1. 主菜单点击「📎 下载类型: 全部」
2. 点击「图片 / 视频 / 文档」分组下的格式名进行多选
3. 或点击「✏️ 手动输入」，直接输入扩展名（逗号分隔，如 mp4,jpg）
4. 点击「☐ 全部」恢复全部下载
```

可选类型：

| 分组 | 扩展名 |
|------|--------|
| 📷 图片 | jpg, png, gif, webp, bmp, svg, tiff |
| 🎬 视频 | mp4, mkv, avi, mov, wmv, flv, ts, webm, m4v |
| 📄 文档 | pdf, doc, docx, txt, zip, rar |

## 账号管理

每个用户（管理员）名下可绑定多个 TDL 账号，互相隔离。

| 操作 | 说明 |
|------|------|
| 🔐 登录新账号 | 新建 TDL 会话，输入手机号 + 验证码 + 2FA（如有） |
| 🔗 绑定现有账号 | 绑定服务器上已登录的 TDL 账号 |
| 🗑️ 删除 TDL 账号 | 从你名下移除某个账号 |
| 📜 查看账号列表 | 查看你的所有账号 |
| @账号名 | 点击账号名切换当前使用的账号 |

## 管理员（仅超级管理员）

- ➕ 添加管理员：输入对方的 Telegram 数字 ID
- ➖ 删除管理员：移除管理员权限
- 📋 查看管理员列表

每个管理员只能看到和使用自己绑定的 TDL 账号。

## 命令速查

| 命令 | 说明 |
|------|------|
| `/start` / `/menu` | 打开主菜单 |
| `/single` | 单文件下载 |
| `/multi` | 批量下载 |
| `/cancel` | 取消当前任务 |
| `/user` | 用户管理 |
| `/admin` | 管理面板（仅超级管理员） |
| `/tdl_list` | 查看 TDL 账号列表 |

## 文件结构

```
├── .github/workflows/docker-publish.yml   # 自动构建并推送镜像到 Docker Hub
├── Dockerfile
├── docker-compose.yml   # 密钥配置在此文件（占位符，部署前改）
├── tdl.py             # 机器人主程序
├── requirements.txt
├── .gitignore
├── .dockerignore
├── RELEASE_NOTES.md
└── README.md
```

## 注意事项

- 所有链接必须是 `https://t.me/c/` 或 `https://t.me/` 格式
- 消息 ID 必须是纯数字
- 批量下载消息越多耗时越长，请耐心等待
- 下载文件保存到 `DL_BASE_PATH/{频道ID}/` 目录下
- 下载过程中点击「❌ 取消」或发送 `/cancel` 可中止任务
