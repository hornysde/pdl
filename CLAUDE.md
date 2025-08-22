Install for development

```
pip install -e ".[dev]" -c requirements.txt
```

Update environment lock after changing dependencies

```
pip-compile pyproject.toml
```

Format and lint after making code changes

```
ruff check --fix
ruff format
mypy *.py
```
