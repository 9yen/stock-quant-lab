# Development

Use Python 3.12+ and Node.js 22+. Windows is required for broker process supervision.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,alpaca,research]"
.\.venv\Scripts\python.exe -m quant_lab demo
```

Install `.[moomoo,tiger]` into a separate `.venv-brokers` environment. Dependencies live in `pyproject.toml`; `requirements*.txt` are compatibility shims.

```powershell
.\.venv\Scripts\ruff.exe check src tests daily_optimize.py
.\.venv\Scripts\ruff.exe format --check src tests daily_optimize.py
.\.venv\Scripts\python.exe -m unittest discover -s tests
node --test tests/test_i18n.cjs
.\.venv\Scripts\python.exe -m build
```

CI runs behavior tests on Windows/Python 3.12 and 3.13, lint/format/JS checks on Linux, and a separate wheel-install smoke test outside the checkout. It needs no broker keys. Ruff is pinned so formatting does not drift between machines.

Signal logic belongs in `strategies/`; SDK calls and broker-specific reconciliation belong in the relevant adapter. Orchestration belongs in `services/`, not HTTP handlers. New persistence should use `runtime.storage.write_json`; order state must stay durable. Test failure recovery and risk behavior with synthetic inputs.

Translations live in `src/quant_lab/dashboard/assets/locales/`. Unknown broker messages retain source text. Earlier research exercises live under `examples/legacy/` and do not define application behavior.
