FROM python:3.12-slim

# 发布流水线用 git tag 覆盖（v0.1.1 → 0.1.1），使 /healthz 与镜像标签一致
ARG APP_VERSION=0.0.0-dev

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_VERSION=${APP_VERSION} \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

COPY requirements.txt ./
# 顺带验证声明的依赖真的能 import：httpx 是模型座位的运行时依赖，
# 只写在 requirements-dev 里会让镜像"能启动、一对话就 ModuleNotFoundError"。
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import fastapi, uvicorn, diplomacy, httpx"

COPY app ./app

RUN useradd --create-home --uid 10001 dw \
    && mkdir -p /data \
    && chown -R dw:dw /data /app

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# 刻意不写 USER：入口脚本以 root 修正数据目录属主（绑定挂载常带进 root:root），
# 再用 setpriv 降权到 uid 10001 执行，进程本身始终以非 root 运行。
# 如需跳过这一步，用 `docker run --user 10001:10001`。

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3).status==200 else 1)"

# 必须单 worker：Arena 是进程内单例，多 worker 会各自持有一份对局状态
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "app.main"]