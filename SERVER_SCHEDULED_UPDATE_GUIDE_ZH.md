# WHO ICTRP 数据库服务器部署与定时更新指南

本指南面向 Ubuntu 24.04。定时任务会先构建 staging 数据库，完成 SQLite、外键、表结构、FTS 和记录数校验后再原子替换生产库。下载、构建或校验失败时，现有生产数据库不会被替换。

## 1. 上传仓库

推荐直接通过 Git 获取完整仓库，避免漏传 `schemas/`、`config/` 或脚本：

```bash
git clone https://github.com/OWNER/REPOSITORY.git ~/who_ictrp_mcp
cd ~/who_ictrp_mcp
```

已有部署可用 `git pull` 更新。不要上传数据库、XML、API Key、虚拟环境或历史 ZIP；这些内容已由 `.gitignore` 排除。

## 2. 安装依赖

```bash
cd ~/who_ictrp_mcp
sudo apt update
sudo apt install -y python3-venv xvfb
python3 -m venv mcp_service/venv
mcp_service/venv/bin/pip install -r requirements.txt
mcp_service/venv/bin/pip install -r mcp_service/requirements.txt
mcp_service/venv/bin/python -m playwright install chromium
```

如果 Playwright 报缺少系统库：

```bash
sudo mcp_service/venv/bin/python -m playwright install-deps chromium
```

## 3. 运行非生产烟雾测试

```bash
cd ~/who_ictrp_mcp
chmod +x deployment/*.sh
deployment/server-smoke-test.sh
```

该测试只执行搜索策略中的第一项，输出到 `data/scheduled-smoke.db`，不会替换生产库。成功标志包括：XML 格式有效、至少导入一条试验、完整性与外键检查通过、FTS 行数与主表一致。

测试后可清理临时库：

```bash
rm -f data/scheduled-smoke.db data/scheduled-smoke.db-wal data/scheduled-smoke.db-shm
```

## 4. 安装 MCP 服务（新服务器）

如果服务器还没有 `who-mcp.service`，先在生产数据库存在后运行：

```bash
cd ~/who_ictrp_mcp
deployment/install-mcp-systemd.sh
```

安装器会创建 `/etc/who-mcp.env` 和随机 API Key；已有该文件时不会覆盖或轮换 Key。仅在需要交给测试人员时读取：

```bash
sudo sed -n 's/^WHO_MCP_API_KEY=//p' /etc/who-mcp.env
```

本机协议测试：

```bash
export WHO_MCP_API_KEY="$(sudo sed -n 's/^WHO_MCP_API_KEY=//p' /etc/who-mcp.env)"
deployment/test-mcp-http.sh
unset WHO_MCP_API_KEY
```

该测试要求无 Key 返回 401、有效 Key 的 `initialize` 请求返回 200。公网反向代理不属于数据库仓库；部署公网端点时应另行配置 HTTPS。

## 5. 安装定时任务

安装脚本会根据当前登录用户、仓库绝对路径和虚拟环境生成 systemd 单元，不需要手工修改用户名：

```bash
cd ~/who_ictrp_mcp
deployment/install-systemd.sh
systemctl list-timers who-db-refresh.timer --all
```

默认计划为每周二 23:30（Asia/Shanghai），并带最多 30 分钟随机延迟。修改 `deployment/who-db-refresh.timer` 后重新运行安装脚本即可。

每月 1 日和 15 日 02:00 可使用：

```ini
OnCalendar=*-*-01,15 02:00:00 Asia/Shanghai
```

## 6. 首次正式运行

```bash
sudo systemctl start who-db-refresh.service
```

另开终端查看日志：

```bash
sudo journalctl -u who-db-refresh.service -f
```

systemd 在后台运行，关闭 SSH 窗口不会中断。完整 WHO 导出可能耗时数小时。

完成后检查：

```bash
systemctl status who-db-refresh.service --no-pager -l
cat data/reports/scheduled_refresh_status.json
mcp_service/venv/bin/python scripts/verify_production_db.py \
  --db data/who_ictrp_cancer_trials.db
```

状态文件应为 `"status": "promoted"`，数据库验证输出应为 `"passed": true`。

## 7. 验证 MCP 服务

数据库更新成功后，systemd 会尝试重启已有的 `who-mcp.service`。检查：

```bash
systemctl status who-mcp.service --no-pager -l
```

使用现有 MCP 客户端依次调用：

1. `database_metadata`：确认 `database_as_of` 或 `source_searched_through` 为本次更新时间。
2. `execute_search_plan`：执行一条小规模检索。
3. `get_trial`：读取上一步返回的一个真实试验 ID。

数据库更新不会更改 MCP API Key。MCP 重启期间可能短暂出现连接失败，客户端应对 502、503、504 做有限重试。

## 8. 失败处理与恢复

查看失败详情：

```bash
sudo journalctl -u who-db-refresh.service --since today --no-pager -l
cat data/reports/scheduled_refresh_status.json
```

若 WHO 下载和 staging 构建已经完成，仅发布校验因代码问题失败，可在修复代码后复用 staging：

```bash
mcp_service/venv/bin/python scripts/refresh_production_who_db.py \
  --production-db data/who_ictrp_cancer_trials.db \
  --database-mode auto \
  --use-existing-staging
sudo systemctl restart who-mcp.service
```

该命令不会跳过完整性、外键、结构、FTS、元数据和记录数检查。

最近三个生产库备份位于 `data/backups/`。需要回滚时：

```bash
sudo systemctl stop who-mcp.service
cp data/backups/选定备份.db data/who_ictrp_cancer_trials.db
sudo systemctl start who-mcp.service
```

## 9. 混合数据库注意事项

更新器默认使用 `--database-mode auto`。如果生产库包含 ClinicalTrials.gov 或 CTIS 来源，更新器会拒绝用 WHO 单源库覆盖它，并要求对应源数据库存在。默认位置可通过 `refresh_production_who_db.py --help` 查看，也可在 systemd 模板的 `ExecStart` 中显式传入 `--ctgov-db` 和 `--ctis-db`。

本定时任务只负责刷新 WHO 层；其他注册库应由各自的构建项目独立更新。
