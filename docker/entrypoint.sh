#!/bin/sh
# 容器入口：修正数据目录属主后降权运行。
#
# 为什么需要这一步：绑定挂载会把宿主目录的属主（通常 root:root）原样带进容器，
# 而进程以 uid 10001 运行，写入 state.json 时直接 PermissionError → 开局 500。
# 命名卷不受影响（Docker 会把镜像内 /data 的属主复制进新卷），但用户最常见的是
# `-v "$PWD/data:/data"`，所以必须在这里兜住。
set -eu

DATA_DIR="${DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    if ! chown -R 10001:10001 "$DATA_DIR"; then
        echo "warn: 无法修改 $DATA_DIR 的属主。若随后出现 PermissionError，" >&2
        echo "      请改用命名卷，或在宿主机执行 chown 10001:10001 <目录>。" >&2
    fi
    export HOME=/home/dw
    exec setpriv --reuid=10001 --regid=10001 --clear-groups "$@"
fi

# 已由 `--user` / compose `user:` 指定非 root 时，直接执行
exec "$@"
