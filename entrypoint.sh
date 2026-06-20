#!/bin/sh
set -e

echo "[entrypoint] 等待数据库就绪..."
python - <<'PY'
import os
import time

import pymysql

cfg = dict(
    host=os.getenv("DB_HOST", "db"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "show"),
    password=os.getenv("DB_PASSWORD", "show123"),
    database=os.getenv("DB_NAME", "show_ticketing"),
)
for i in range(60):
    try:
        conn = pymysql.connect(**cfg)
        conn.close()
        print("数据库已就绪")
        break
    except Exception as exc:  # noqa: BLE001
        print(f"等待数据库... ({i + 1}) {exc}")
        time.sleep(2)
else:
    raise SystemExit("数据库在超时时间内未就绪")
PY

echo "[entrypoint] 检测旧数据库结构..."
python - <<'PY'
import os

import pymysql

cfg = dict(
    host=os.getenv("DB_HOST", "db"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "show"),
    password=os.getenv("DB_PASSWORD", "show123"),
    database=os.getenv("DB_NAME", "show_ticketing"),
)
conn = pymysql.connect(**cfg)
cur = conn.cursor()
cur.execute("SHOW TABLES LIKE 'settlement_parties'")
has_new_tables = cur.fetchone() is not None
cur.execute("SHOW TABLES LIKE 'ticket_orders'")
has_old_tables = cur.fetchone() is not None

if has_old_tables and not has_new_tables:
    print("检测到旧数据库结构（缺少分账系统表），自动重置所有表...")
    cur.execute("SET FOREIGN_KEY_CHECKS=0")
    cur.execute("SHOW TABLES")
    rows = cur.fetchall()
    for (tbl,) in rows:
        cur.execute(f"DROP TABLE `{tbl}`")
    cur.execute("SET FOREIGN_KEY_CHECKS=1")
    conn.commit()
    print(f"已重置 {len(rows)} 张表，等待迁移重建")
else:
    print(f"数据库结构正常（has_new={has_new_tables}, has_old={has_old_tables}）")
cur.close()
conn.close()
PY

echo "[entrypoint] 应用数据库迁移..."
python manage.py migrate --noinput

echo "[entrypoint] 初始化种子数据..."
python manage.py seed

echo "[entrypoint] 启动服务..."
exec gunicorn config.wsgi:application --bind 0.0.0.0:7652 --workers 2
