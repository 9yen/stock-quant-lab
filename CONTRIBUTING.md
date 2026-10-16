# Contributing

English and Chinese contributions are welcome. This is a learning project, not a return-maximization service.

1. Open an issue explaining the behavior or learning question. Use synthetic or redacted examples.
2. Keep changes small. Preserve PAPER/SIMULATE boundaries, stop flags, reconciliation and entry limits.
3. Add tests for meaningful behavior, especially ambiguous orders, partial fills and risk exits.
4. Run Python tests and the JavaScript localization tests before opening a pull request.

```powershell
python -m unittest discover -s tests
node --test tests/test_i18n.cjs
```

Never upload `.env`, account IDs, private keys, real fills, logs, gateway credentials or personal screenshots. Do not run broker-connected tests in CI. Do not claim a backtest or paper session proves live profitability.

## Translations

Catalogs live in `src/quant_lab/dashboard/assets/locales/`. `zh-CN.json` contains source strings; `en.json` has the same keys with English translations. Keep placeholders, percentages, ticker symbols and broker mode tokens intact. For another locale, add a catalog, explicitly whitelist its route in `src/quant_lab/dashboard/web.py`, register the locale in `src/quant_lab/dashboard/assets/i18n.js`, and add it to the selectors on both pages. Update catalog parity and coverage tests.

The shared DOM adapter supports legacy dynamic renderers without changing account data. New UI code should use `window.QuantI18n.t(source)` at display boundaries. Browser choices are saved only in local storage.
