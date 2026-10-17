# Development

Use Python 3.12+ and Node.js 22+. Native development supports Windows; the complete
Linux runtime is tested in Docker. See [deployment](DOCKER.md) before connecting accounts.

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

CI runs behavior tests on Windows/Python 3.12 and 3.13 and in the installed Linux
paper image, plus both Docker deployment smoke checks, privacy checks, lint/format/JS
checks and a wheel-install smoke test outside the checkout. Only the optional native
Tk window test is skipped in the headless web image. No CI job needs broker keys.
Ruff and Docker runtime dependencies are pinned so versions do not drift silently.

Signal logic belongs in `strategies/`; SDK calls and broker-specific reconciliation belong in the relevant adapter. Orchestration belongs in `services/`, not HTTP handlers. New persistence should use `runtime.storage.write_json`; order state must stay durable. Test failure recovery and risk behavior with synthetic inputs.

Translations live in `src/quant_lab/dashboard/assets/locales/`. Unknown broker messages retain source text. Earlier research exercises live under `examples/legacy/` and do not define application behavior.
