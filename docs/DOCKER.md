# Docker deployment / Docker 部署

## Two deployment modes / 两种部署

| Compose file | Purpose | Credentials / orders |
| --- | --- | --- |
| `compose.yaml` | Synthetic demo, port 8767 | None / none |
| `compose.paper.yaml` | Complete PAPER runtime, port 8766 | Local binding / simulation only |
| `compose.host.yaml` | Optional Desktop host-network override | OpenD stays on loopback |

The full runtime includes the web desk, continuous Alpaca session manager, moomoo
scanner/observer/options watcher/executor, and periodic bounded strategy review.
One container shares a PID namespace and Linux locks across these workers. The
supervisor retries exited workers with backoff; health checks detect stale supervision
and missing workers. A living worker is not killed just because its broker is offline.
Docker restarts exited containers; `unhealthy` by itself does not trigger a restart.

完整后台使用一个容器管理多个独立进程：网页、Alpaca 会话管理、moomoo 行情/账户/期权观察与模拟
执行，以及周期复盘。进程各自工作，共享同一套日志、风险记录和文件锁。连接故障会进入等待状态，
不会自动清除未成交订单、停止标记或每日亏损限制。软件止损依赖后台运行，不能保证成交价格。

## Demo / 无账户演示

Start Docker Desktop in Linux-container mode, then run in the repository:

```sh
docker compose up -d --build --wait
docker compose ps
docker compose logs --tail 100 -f demo
docker compose down
```

Open <http://127.0.0.1:8767/>. The demo never mounts credentials or account state.
It does not replace native trading on 8766. `down` retains the isolated log volume;
synthetic balances and candles are regenerated rather than saved as trade history.

## Full paper runtime / 完整模拟后台

### Prepare once / 首次迁移

1. Confirm positions and pending orders directly with both brokers. Prefer a flat,
   quiet period. Resolve ambiguous orders before migration; never reset the ledger.
2. Verify OpenD is logged in and reachable from a test container. For Desktop host
   networking, explicitly enable Settings → Resources → Network → Enable host networking.
   The override below connects to the host's loopback without changing OpenD bindings
   or firewall rules. This grants host-network access to that container.
3. Stop native supervision and its workers. Disable its login shortcut and any native
   scheduled optimizer before using containers against the same accounts. Windows
   byte locks and Linux locks must not be assumed to coordinate across environments.
4. Copy the existing private runtime with the unified migration command. Use a **new
   private directory outside every Git checkout**, not a project subdirectory.

首次切换前需要确认账户状态、网关登录与连通性，再停止旧后台及其登录启动项和复盘计划。
不要同时运行两套执行器。迁移工具拒绝仍有本项目工作进程的源目录，保留原目录作为回退副本，
复制账户授权、历史记录、日志、风险状态和 STOP 标记，最后生成仅限模拟盘的容器授权文件。
它不会替你查询账户是否空仓；账户确认必须在迁移前完成。

Example in PowerShell (replace the source with your local project):

```powershell
$env:QUANT_LAB_CONTAINER_HOME = Join-Path $env:LOCALAPPDATA 'StockQuantLabDocker'
python -m quant_lab migrate-container --source . --destination $env:QUANT_LAB_CONTAINER_HOME --activate-paper
```

The migration command refuses an existing destination or a directory inside a Git
repository. A failed copy remains unactivated. Review the copied `.env` and account
bindings locally; never paste them into issues, screenshots, CI settings or commits.
The Compose runtime requires a local `.env` file (empty is acceptable for moomoo-only
installations). On native Linux, grant UID/GID 10001 access to this private directory;
keep access restricted to the operator. Do not use world-writable permissions.

### Start / 启动

Bridge mode, if OpenD already supports the configured endpoint:

```powershell
docker compose -f compose.paper.yaml up -d --build --wait --wait-timeout 120
```

Docker Desktop with the explicitly enabled host networking feature:

```powershell
docker compose -f compose.paper.yaml -f compose.host.yaml up -d --build --wait --wait-timeout 120
```

Open <http://127.0.0.1:8766/>. Both modes keep the web desk local: bridge mode publishes
127.0.0.1; host mode binds the application itself to 127.0.0.1. Do not change the host
port without updating the application's port setting: Host/Origin validation is exact.
An empty runtime or missing migration marker blocks the default execution entrypoint.
Existing account authorization is still required for each enabled broker.

On Windows, `start_docker.cmd` uses the default private runtime directory and the host
network override. After migration, `start_automatic.cmd` routes to the same Docker
launcher. A marker in the old native state blocks its execution commands, preventing
an accidental restart against stale ledgers. Do not delete that marker to bypass a hold.

`QUANT_LAB_OPEND_HOST` and `QUANT_LAB_OPEND_PORT` configure bridge-mode gateway access.
Native Windows defaults remain 127.0.0.1:11111. OpenD login remains an external dependency;
this project does not package an account login or trading-unlock password in an image.
Docker Desktop must run after reboot; its restart policy cannot start the engine itself.

### Operate / 日常操作

Use the same `-f` options that you used when starting:

```powershell
docker compose -f compose.paper.yaml -f compose.host.yaml ps
docker compose -f compose.paper.yaml -f compose.host.yaml logs --tail 100 -f lab
docker compose -f compose.paper.yaml -f compose.host.yaml exec lab quant-lab status --json
```

The private `logs/` directory retains per-worker output and trading audit records.
Docker stdout has bounded rotation. Keep logs and diagnostic exports private: broker
SDKs and account snapshots can contain account information even when application
lifecycle errors only report their type.

For a planned stop, use the dashboard's stop controls first, wait for both brokers to
confirm flat/no pending orders, and then run `down`. `down`, process termination,
reboots and container restarts do **not** guarantee position liquidation. After an
unexpected restart, the original ledgers and broker reconciliation govern recovery.
Do not remove the runtime directory or clear STOP flags to solve an error.

停止前先在网页请求停止，再到券商确认空仓且无挂单，最后停止容器。直接关闭 Docker 不等于平仓。
发生故障后保持原交易记录，让程序对账恢复；不要通过删除状态文件“修复”。

Periodic review runs every 30 minutes in the container. It uses the existing bounded
stock-filter evaluation; `optimizer_auto_apply: true` in private `automation_config.json`
is required to promote a qualifying profile. It does not loosen position, loss or
permission limits. Set `review_enabled: false` to disable this worker. The review status
is private `state/review_status.json`; data/calendar failures are reported as holds.

### Update and rollback / 更新与回退

Back up the **private runtime** separately before updating. Inspect the release and run
offline tests, then rebuild with the same Compose files. Update while flat whenever
possible. Code rollback does not roll back broker fills. Never restore an old ledger
over newer fills. To return to native execution, stop the container first, reconcile
both accounts and transfer the newest private state back before enabling native workers.
The migration tool intentionally refuses overwriting a destination.
Only after stopping containers, reconciling accounts and restoring the newest state
may an operator remove the old native migration marker to perform an intentional rollback.

## Build and privacy / 构建与隐私

The Python base image is pinned by digest. `requirements-docker.lock` records tested
Linux/Python 3.12 dependency versions; it is not a Windows dependency file. Update it
explicitly and rerun SDK imports, regression tests and both image smoke checks. Build
and runtime layers are separated so source changes reuse dependency installation.

The build context is an allowlist of source, package metadata and the dependency lock.
No `.env`, runtime state, logs, backups, Git history or virtual environment enters the
image. The runtime home is mounted separately, and `.env` is overmounted read-only.
Images run as UID/GID 10001 with a read-only root filesystem and writable private data
and temporary mounts. Images are built locally; no image registry upload is configured.

Run `python scripts/check_public.py` before publication. CI runs the same guard and
reports file/line/category only, never matched values. This is a guard against common
mistakes, not proof that every possible identifier is absent. Review public screenshots
as synthetic and inspect the staged diff too. Do not upload the private runtime as an
Actions artifact or attach raw account diagnostics to public issues.

构建上下文使用白名单，镜像与公开仓库不包含你的密钥、账户绑定、交易记录或本地备份。
隐私检查只报告文件位置和问题类型，不打印命中内容。公开截图只使用合成演示数据。
