#!/usr/bin/env python3
"""一条命令启动村镇便民点水费代缴系统。

    python3 app.py                # 默认 http://127.0.0.1:8000
    PORT=8080 python3 app.py      # 自定义端口

首次启动自动建库并写入演示数据（含错户冲正链、现金短款、回执延迟样例）。
删除 data/ 目录即可恢复全新演示环境。
"""
from web.server import run

if __name__ == "__main__":
    run()
