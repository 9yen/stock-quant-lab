# Docker deployment / Docker 部署

## Supported now / 目前支持

The Linux image runs the **synthetic demo**, with both browser desks, no credentials
and no broker SDKs. It is a reproducible preview for contributors. It does **not**
replace Windows paper execution yet. The native trading workers remain unchanged.

当前 Linux 镜像用于**合成行情演示**，包含两个交易台，不读取账户，也不下单。
可用于分享项目、验证界面。Windows 模拟交易仍使用原来的启动方式；不要删除现有启动项。

Install and start Docker Desktop in Linux-container mode, then run in the repository:

```sh
docker compose up -d --build --wait
docker compose ps
```

Open <http://127.0.0.1:8767/>. Existing native trading on port 8766 is unaffected.
The published port is deliberately restricted to this computer. Keep the host port
8767: the dashboard validates the HTTP Host header against its listening port.

先启动 Docker Desktop，再在项目目录运行以上命令。打开 8767 查看演示；8766 仍是原来的模拟盘。
容器内部监听所有网卡是为了端口转发，宿主机只发布到 127.0.0.1。不要改成公网发布。

```sh
docker compose logs --tail 100 -f demo
docker compose restart demo
docker compose down
```

`down` stops the demo and retains its isolated log volume. Demo candles and account
values are regenerated, not a trade-history backup. Avoid `down -v` if logs are needed.
Docker's restart policy works when the Docker engine is running; Docker Desktop
still needs to start after login/reboot. These commands do not install a login task.

`down` 停止演示但保留独立日志卷；合成账户和行情每次重建，不用于保存真实模拟盘历史。
Docker 引擎运行后重启策略才生效，开机时仍需要启动 Docker Desktop。

## Isolation / 数据隔离

The build context uses an allowlist. `.env`, state, logs, backups, virtual environments
and Git history are excluded. Compose mounts only a new demo volume; it does not
mount the Windows project, credentials, order ledger or OpenD socket. No executor is
started. Do not override the image command to run paper executors: this image does
not include their SDKs or Linux process supervision support.

构建只允许源码和包元数据进入镜像，不复制密钥或交易状态。不要把现有运行目录挂进此演示容器，
也不要通过修改容器命令尝试开启自动交易。

## Remaining execution migration / 自动交易迁移待办

- Replace Windows process inventory, child launch paths and byte locks with tested
  Linux equivalents; validate restart/reconciliation with isolated paper fixtures.
- Keep each broker's account binding, intent ledger, stop flags and session loss
  baseline intact. Windows and Linux locks cannot be assumed to coordinate.
- Design cross-container status/health checks: process IDs do not identify workers
  in another container's PID namespace.
- Make OpenD host/port configurable, validate Docker-to-host connectivity and its
  authentication/encryption requirements. Do not broaden OpenD's listen address
  or firewall rules automatically. OpenD still needs a logged-in session.
- Stop native executors before activating container executors against the same
  account. A successful demo health check is not evidence that trading is running.

交易执行器迁移还需要跨平台锁、进程恢复、跨容器健康状态和 OpenD 连接验证。
切换前必须停掉原执行器并完整迁移账户绑定、未成交意图和风险状态，避免两套程序重复下单。
