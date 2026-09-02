# WHO MCP 与 CancerDAO 平台同机部署

本方案让 WHO MCP 仅监听 `127.0.0.1:18080`，平台通过本机 HTTP 调用。MCP 使用独立
`who-mcp` 用户，不能读取 CancerDAO 平台的环境文件。迁移期间保留旧服务器，完成真实
检索测试后再停止旧服务。

## 资源要求

建议至少保留 15 GB 磁盘、4 GB 内存和 2 个 CPU 核心。刷新服务默认保留一份生产库
备份，并在成功或失败后删除未被当前生产数据库引用的历史 XML。

## 安装

```bash
sudo useradd --system --create-home --home-dir /var/lib/who-mcp \
  --shell /usr/sbin/nologin who-mcp || true
sudo apt update
sudo apt install -y git python3-venv xvfb rsync
sudo git clone https://github.com/CancerDAO/WHO-db-timer.git /opt/who-db-timer
sudo python3 -m venv /opt/who-db-timer/mcp_service/venv
sudo /opt/who-db-timer/mcp_service/venv/bin/pip install \
  -r /opt/who-db-timer/requirements.txt
sudo /opt/who-db-timer/mcp_service/venv/bin/pip install \
  -r /opt/who-db-timer/mcp_service/requirements.txt
sudo install -d -o who-mcp -g who-mcp \
  /opt/who-db-timer/data/raw_xml \
  /opt/who-db-timer/data/reports \
  /opt/who-db-timer/data/backups
sudo -u who-mcp -H /opt/who-db-timer/mcp_service/venv/bin/python \
  -m playwright install chromium
```

如 Playwright 报缺少系统库：

```bash
sudo /opt/who-db-timer/mcp_service/venv/bin/python \
  -m playwright install-deps chromium
```

## 迁移数据库

将旧服务器生产库传输到 `.incoming` 文件，分别执行 `sha256sum` 并确认哈希一致，再执行：

```bash
sudo mv /opt/who-db-timer/data/who_ictrp_cancer_trials.db.incoming \
  /opt/who-db-timer/data/who_ictrp_cancer_trials.db
sudo chown who-mcp:who-mcp \
  /opt/who-db-timer/data/who_ictrp_cancer_trials.db
```

不迁移旧服务器的 `backups/`、`raw_xml/`、虚拟环境或烟雾测试数据库。

## 安装服务

```bash
cd /opt/who-db-timer
sudo env \
  WHO_DB_PROJECT_ROOT=/opt/who-db-timer \
  WHO_DB_SERVICE_USER=who-mcp \
  WHO_DB_SERVICE_GROUP=who-mcp \
  WHO_DB_PYTHON=/opt/who-db-timer/mcp_service/venv/bin/python \
  deployment/install-mcp-systemd.sh
sudo env \
  WHO_DB_PROJECT_ROOT=/opt/who-db-timer \
  WHO_DB_SERVICE_USER=who-mcp \
  WHO_DB_SERVICE_GROUP=who-mcp \
  WHO_DB_PYTHON=/opt/who-db-timer/mcp_service/venv/bin/python \
  deployment/install-systemd.sh
```

安装器在 `/etc/who-mcp.env` 生成新 API Key。不要复用、打印或提交旧服务器的 Key。

## 平台切换

在 `/home/cancerdao/.cancerdao/env` 中配置：

```text
WHO_MCP_URL=http://127.0.0.1:18080/mcp
WHO_MCP_API_KEY=<读取自 /etc/who-mcp.env>
```

删除 `WHO_MCP_ALLOW_INSECURE_HTTP=1`。本机回环 HTTP 不需要该授权，也不要开放18080公网端口。

## 验证

```bash
sudo systemctl status who-mcp.service --no-pager -l
sudo ss -lntp | grep 18080
sudo bash -c 'set -a; source /etc/who-mcp.env; set +a; \
  cd /opt/who-db-timer; deployment/test-mcp-http.sh'
sudo -u who-mcp -H /opt/who-db-timer/mcp_service/venv/bin/python \
  /opt/who-db-timer/scripts/verify_production_db.py \
  --db /opt/who-db-timer/data/who_ictrp_cancer_trials.db
```

确认平台真实临床匹配成功后，再停止旧服务器 timer 和 MCP。首次定时刷新应人工启动并观察：

```bash
sudo systemctl start who-db-refresh.service
sudo journalctl -u who-db-refresh.service -f
```

`scheduled_refresh_status.json` 的正常最终状态为 `promoted`，且
`post_promotion_smoke.passed` 为 `true`。该检查覆盖 MCP 初始化、数据库水位线和一条
最小癌症检索。若新 MCP 启动、协议或查询检查失败，状态为
`rolled_back`；服务会重新使用上一份生产数据库。
