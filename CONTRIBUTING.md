# Contributing

Issues and pull requests are welcome at <https://github.com/diogodias24/ha-climate-adapt>.

## Development setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements_dev.txt
```

## Checks

Run these before opening a pull request; CI runs the same commands.

```bash
make check   # ruff, mypy, bandit
make test    # pytest
```

### Pinned lint Home Assistant version

The CI lint job pins `pytest-homeassistant-custom-component==0.13.375` (which pins Home Assistant
2026.10.4) so that mypy results are reproducible. `mypy.ini` disables `arg-type` for
`climate_adapt.config_flow` only, because Home Assistant 2026.10+ types `data_schema` as a
`probatio` schema while the config flow still builds `voluptuous` schemas.

To bump the pin: choose a newer `pytest-homeassistant-custom-component` release, change the version in
`.github/workflows/test.yml` and above, install it in a clean venv, and run `make check`. Remove the
`mypy.ini` override once mypy is green without it.

Tests run against a real Home Assistant core through `pytest-homeassistant-custom-component`.
New behaviour needs a test, and a bug fix needs a test that fails without the fix.

## Pull requests

- Keep a change focused on one behaviour.
- Describe what changes for the user and why.
- Do not include personal data (names, addresses, real entity ids, coordinates, tokens) in code,
  tests, fixtures or issues. Use synthetic ids such as `climate.living_room`.
- Do not commit recorded household data (sensor or unit history, schedules, routines, exact
  timestamps of real events), not even resampled or with noise added. Build test inputs in the test.
- By contributing you agree that your contribution is licensed under Apache-2.0, like the rest of the project.
