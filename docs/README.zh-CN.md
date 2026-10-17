# Stock Quant Lab · 中文使用说明

[English](../README.md) | 简体中文

这是用于学习交易基础设施的本地模拟盘项目。0.2 版把散落脚本整理为可安装的 Python 包，采用统一入口、分层目录、独立运行状态和自动化质量检查。没有开启真实资金交易。

## 安装与无密钥演示

也可以先启动 Docker Desktop，再运行：

```sh
docker compose up -d --build --wait
```

打开 http://127.0.0.1:8767/。目前容器提供合成行情演示，交易执行器仍在 Windows 运行。
部署范围、日志和后续迁移说明见 [Docker 部署](DOCKER.md)。

需要 Python 3.12 或以上。自动交易进程管理目前支持 Windows；网页后端不再依赖 Tk 桌面组件。

```powershell
git clone https://github.com/9yen/stock-quant-lab.git
cd stock-quant-lab
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m quant_lab demo
```

打开 http://127.0.0.1:8767/。这是合成行情演示，不连接账户、不读取密钥、不下单。中英文可在右上角切换。

账户读取、研究与开发按需安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,alpaca,research]"
python -m venv .venv-brokers
.\.venv-brokers\Scripts\python.exe -m pip install -e ".[moomoo,tiger]"
```

依赖统一定义在 `pyproject.toml`；两个 requirements 文件只保留兼容入口。

## 常用命令

以下使用相应虚拟环境的 Python，也可以使用安装后生成的 `quant-lab` 命令。

| 命令（接在 `python -m quant_lab` 后） | 用途 |
| --- | --- |
| `--help` | 查看统一命令入口 |
| `doctor --json` | 检查安装、资源、SDK 和配置；不连接券商 |
| `status --json` | 只读查看本地守护状态 |
| `demo` | 无账户演示，默认 8767 |
| `dashboard` | 真实模拟账户看板，默认 8766；本身不启动下单 |
| `supervise` | 管理已授权的本地模拟进程 |
| `run-alpaca --execute-paper` | 已授权 Alpaca 连续场次管理 |
| `run-moomoo --execute-paper` | 已授权 moomoo SIMULATE 执行 |
| `optimize --auto-apply` | 闭市复盘和受证据门槛限制的参数评估 |

新克隆的代码不带账户绑定、已启用配置、行情样本或 Windows 登录任务，不会自动获得交易权限。先配置并核查自己的模拟账户，不要复制别人的状态账本。

## 已配置本机版本

原有看板地址仍为 http://127.0.0.1:8766/ 和 http://127.0.0.1:8766/moomoo.html 。根目录 `start_automatic.cmd` 保留给已有 Windows 登录快捷方式；`daily_optimize.py` 保留给已有本地每日任务，业务实现均已移入包内。

关闭网页不会停止后台。守护程序恢复退出的进程，但不会清除手动停止标记或当日风控锁定。电脑需唤醒联网；moomoo OpenD 需在本机 11111 端口登录。停止请求必须以券商持仓、撤单和成交状态确认完成。

## 配置与数据位置

`--home PATH`（放在子命令前）或 `QUANT_LAB_HOME` 决定运行目录。源码安装默认使用项目目录；wheel 安装默认使用用户本地的 StockQuantLab 目录。

`.env`、`state/`、`logs/`、`STOP_PAPER`、`STOP_MOOMOO` 保持在运行目录中，不放入源码包。可将 `config/local.example.toml` 复制为运行目录的 `config/local.toml`，调整非敏感网页设置。只允许本机回环地址；不支持公开托管多用户交易服务。

不删除或覆盖订单账本来“解决”账户不一致。升级或回滚代码也必须保留最新成交状态。

## 交易与盈亏边界

此次工程重构不调整原规则：100 股观察池、新仓目标 10%、单股硬上限 40%、组合多空绝对敞口 100%、最多 10 个持仓/预留名额；股票止损 1%、止盈 2%。Alpaca 日亏损预算 1%，moomoo 2%；每股每天 19 次、总共 100 次，冷却分别 15/5 分钟。

moomoo 期权为权限通过后的受限买方模拟模块，不是末日期权策略。止盈止损依赖本机在线，无法保证断线期间按计划价格退出。小账户整股取整可能得到零股，固定费用也可能吞掉小仓位收益；模拟盈利不证明真实交易有效。

Alpaca 的 moomoo NZ 扣费后结果是费用模型估算，不是 Alpaca 实际扣费。moomoo 模拟成交费用明细不完整，策略按毛利展示，账户净值变动另算。平仓收益率以配对开仓金额为分母，不是账户日收益率。

## 开发与维护

代码按策略、券商、分析、服务、运行设施和界面分层。新增逻辑应进入对应模块，不要重新堆在根目录脚本。

```powershell
.\.venv\Scripts\ruff.exe check src tests daily_optimize.py
.\.venv\Scripts\ruff.exe format --check src tests daily_optimize.py
.\.venv\Scripts\python.exe -m unittest discover -s tests
node --test tests/test_i18n.cjs
.\.venv\Scripts\python.exe -m build
```

[架构说明](ARCHITECTURE.md) · [开发说明](DEVELOPMENT.md) · [升级迁移](MIGRATION.md) · [版本记录](../CHANGELOG.md)

GitHub CI 检查格式、静态错误、Python 3.12/3.13 行为测试、国际化，以及脱离源码目录的 wheel 安装。CI 不使用券商密钥。测试通过不等于策略盈利得到证明。
