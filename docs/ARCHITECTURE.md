# Architecture

The supported application is an installable `src/` package. Root scripts are not a second implementation. Broker execution stays separate because permissions, order semantics and protective orders differ.

```text
src/quant_lab/
  cli.py                  stable command routing and explicit execution flags
  runtime/                paths, validated settings, atomic storage, service logs
  strategies/             opening range, indicators, entry filters, exits
  brokers/alpaca/         paper account reads, execution, reconciliation, charts
  brokers/moomoo/         gateway reads, SIMULATE execution, options, policy
  analytics/              FIFO P&L, fees, research, bounded daily evaluation
  services/               worker recovery and daily Alpaca session manager
  dashboard/data.py       read-only snapshots; no GUI toolkit import
  dashboard/web.py        loopback HTTP/SSE API with resource allowlist
  dashboard/native.py     optional Tk window
  dashboard/demo.py       isolated synthetic demo
  dashboard/assets/      wheel-packaged HTML, JS, CSS and translations
```

## Runtime boundaries

Installed code/resources are separate from writable account state. Home is selected by `--home`, `QUANT_LAB_HOME`, the source checkout, or a user-local `StockQuantLab` directory for wheel installations. Existing `.env`, `state/`, `logs/`, `STOP_PAPER` and `STOP_MOOMOO` paths remain stable. The browser never serves arbitrary files from runtime home.

`config/local.toml` holds non-secret web settings only. Unknown settings, public native bind addresses and invalid ports fail validation. Bridge containers bind internally to all interfaces with localhost-only publication; the host-network override binds directly to loopback. Broker authorization and risk policy remain in their existing explicit local records.

## Execution and recovery

The CLI imports only the chosen worker. Status, diagnostics and the demo do not contact brokers. Execution retains explicit paper flags and broker/account restrictions.

The supervisor starts workers through `python -m quant_lab` in the appropriate environment. It recognizes old script processes during migration, uses a platform-specific instance lock, and retries exited workers with backoff. It does not kill a living executor with ambiguous orders or erase stops/risk locks.

Intent is persisted before submission. Shared storage writes a unique temporary file, flushes/fsyncs durable state, then atomically replaces the destination. Market snapshots can opt out of fsync; order journals cannot. Failed writes keep the previous ledger.

`logs/services.<command>.jsonl` records lifecycle events and error types with rotation. It excludes raw SDK exception strings and request headers. Existing broker audit logs remain separate for reconciliation and review.

This refactor does not change signals, allocation, stop/take-profit levels, entry limits, calendars or option permissions. It does not claim a generic broker interface makes those semantics interchangeable. Linux containers use `/proc` process inventory and `flock`, with all workers sharing one PID namespace. Packaged web resources are tested independently. The default container entrypoint requires a private migration marker, and the native runtime must be stopped before account cutover.
