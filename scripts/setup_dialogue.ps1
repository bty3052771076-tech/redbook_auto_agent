param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ([System.IO.Path]::GetPathRoot($root) -ne 'E:\') {
    throw 'The private dialogue environment must be installed on E:.'
}
$python = Join-Path $root '.venv\Scripts\python.exe'
$overlay = Join-Path $root 'data\assistant-python'
$names = @('PYTHONPATH', 'PIP_CACHE_DIR', 'TEMP', 'TMP')
$saved = @{}
foreach ($name in $names) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
try {
    $env:PYTHONPATH = $overlay
    $env:PIP_CACHE_DIR = Join-Path $root 'data\pip-cache'
    $env:TEMP = Join-Path $root 'data\tmp'
    $env:TMP = $env:TEMP
    foreach ($path in @($overlay, $env:PIP_CACHE_DIR, $env:TEMP)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
    }
    if (-not $CheckOnly) {
        & $python -m pip install --target $overlay --upgrade --no-deps --disable-pip-version-check -r (Join-Path $root 'deployment\requirements-dialogue.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Private dependency installation failed.' }
    }
    & $python -c "from importlib.metadata import version; from langchain.agents import create_agent; from langchain.agents.middleware import SummarizationMiddleware; from langgraph.checkpoint.postgres import PostgresSaver; expected={'langchain':'1.2.0','langgraph':'1.0.5','langgraph-prebuilt':'1.0.5'}; actual={k:version(k) for k in expected}; assert actual==expected, actual; print(actual); print('Dialogue framework imports OK; shared virtualenv unchanged.')"
    if ($LASTEXITCODE -ne 0) { throw 'Framework compatibility check failed; do not start a generation task.' }
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
}
