FROM python:3.11-slim

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 下载并安装 tdl（latest 重定向，无需 GitHub API）
ARG TARGETARCH
RUN if [ "$TARGETARCH" = "arm64" ]; then \
        ARCH="arm64"; \
    else \
        ARCH="64bit"; \
    fi \
    && TDL_URL="https://github.com/iyear/tdl/releases/latest/download/tdl_Linux_${ARCH}.tar.gz" \
    && echo "Downloading latest tdl for ${ARCH}: $TDL_URL" \
    && curl -fSL "$TDL_URL" -o /tmp/tdl.tar.gz \
    && tar -xzf /tmp/tdl.tar.gz -C /usr/local/bin \
    && chmod +x /usr/local/bin/tdl \
    && rm /tmp/tdl.tar.gz \
    && echo "tdl installed successfully"

# 复制代码
COPY tdl.py download_queue.py .

# 环境变量，防止 tdl 终端卡死
ENV TERM=dumb
ENV NO_COLOR=1

# 启动机器人
CMD ["python3", "-u", "tdl.py"]
