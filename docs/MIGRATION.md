# Migrating to 0.2

This changes source layout and packaging, not strategy or account state.

For an existing installation, inspect broker positions/orders, back up locally, and stop the supervisor and its workers before switching code/environments. Never commit the state backup.

```powershell
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
.\.venv-brokers\Scripts\python.exe -m pip install -e . --no-deps
.\.venv\Scripts\python.exe -m quant_lab doctor --json
```

Keep `.env`, `state/`, `logs/`, `STOP_PAPER` and `STOP_MOOMOO` at the original home. Do not regenerate account bindings or reset journals. Restart using `start_automatic.cmd` or `python -m quant_lab supervise`, then check both broker status cards.

The existing Windows login shortcut still uses root `start_automatic.cmd`. Existing daily tasks may keep using `daily_optimize.py --auto-apply`, a thin compatibility entry point. New tasks should use `python -m quant_lab optimize --auto-apply`.

Do not leave an old root `quant_lab/` directory shadowing the installed `src/` package. Historical root implementations are moved, not copied into a second implementation.

| Previous script | Supported command after `python -m quant_lab` |
| --- | --- |
| `demo_dashboard.py` | `demo` |
| `web_dashboard.py` | `dashboard` |
| `automation_supervisor.py` | `supervise` |
| `alpaca_auto.py --execute-paper` | `run-alpaca --execute-paper` |
| `paper_intraday.py --execute-paper --session DATE` | `alpaca-session --execute-paper --session DATE` |
| `moomoo_auto.py --execute-paper` | `run-moomoo --execute-paper` |
| `moomoo_market.py` | `scan-moomoo` |
| `moomoo_observer.py --watch` | `observe-moomoo --watch` |
| `scan_intraday.py --watch` | `scan-alpaca --watch` |
| `moomoo_options_watch.py` | `watch-options` |
| `daily_optimize.py --auto-apply` | `optimize --auto-apply` |

To roll back, stop new workers first and restore saved code/environment configuration. Keep current journals and account state; do not overwrite newer fills with an older backup. Inspect broker state before restarting.
