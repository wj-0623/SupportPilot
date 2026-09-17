$ErrorActionPreference = "Stop"

& .\.venv\Scripts\ruff.exe format --check app tests scripts
& .\.venv\Scripts\ruff.exe check app tests scripts
& .\.venv\Scripts\python.exe -m mypy app
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m app.evaluation.runner
& .\.venv\Scripts\bandit.exe -q -r app
& .\.venv\Scripts\python.exe -m pip_audit -r requirements.lock --progress-spinner off

Write-Host "SupportPilot V4 quality gate passed."
