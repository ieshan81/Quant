# Quarantined tests

No tests are currently quarantined. The full pytest suite is expected to pass.

## Previously flaky / fixed in acceptance run

| Test | Issue | Resolution |
|------|--------|------------|
| `test_ai_console_tab_in_dashboard` | Asserted removed UI label `"AI Console"` | Updated to `"MoMo Console"` |
| `test_api_broker_diagnostic_*` | Wrote under real `PERSIST_DIR` | Fixture patches `PERSIST_DIR` to `tmp_path` |

## When to quarantine

Add a row here before excluding a test from CI:

- test name (module::function)
- why quarantined
- since commit
- risk if left broken
- condition to restore
- owner module

## CI command

```bash
pytest -q
```

If quarantined tests exist:

```bash
pytest -q -m "not quarantined"
```

(requires `@pytest.mark.quarantined` on those tests)
