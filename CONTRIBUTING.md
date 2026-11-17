# Contributing

Bug reports and pull requests are welcome at
<https://github.com/CakeBrewery/burner>.

## Set up

```
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

burner needs Python 3.11 or newer and runs on macOS and Linux.

## Check a change

```
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest
```

Both must pass before a pull request is merged. CI runs them on macOS and
Linux.

## Rules

- burner uses only the Python standard library at run time. Test and lint
  tools go in the `dev` extra.
- Write a test that fails before the change and passes after it.
- Update `README.md` and `CHANGELOG.md` in the same change when behavior
  changes.
- Tests start real sessions. Give them a run dir of their own, such as the
  `run_dir` fixture or `BURNER_DIR` set to a temporary directory, and stop
  every session they start. Never point a test at `~/.burner`.
- Write docs, comments, and messages in short sentences and plain, active
  words.
