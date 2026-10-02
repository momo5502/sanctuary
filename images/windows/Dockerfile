FROM python:3.13.15-windowsservercore-ltsc2022

SHELL ["C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "-NoLogo", "-ExecutionPolicy", "Bypass", "-Command"]

ARG NODE_VERSION=22.22.3
ARG CODEX_VERSION=latest
ENV NPM_CONFIG_PREFIX="C:\npm-global"
ENV PATH="C:\nodejs;C:\npm-global;C:\Python\Scripts;C:\Python;C:\Windows\system32;C:\Windows;C:\Windows\System32\Wbem;C:\Windows\System32\WindowsPowerShell\v1.0\;C:\Windows\System32\OpenSSH"
RUN $ErrorActionPreference = 'Stop'; \
    $ProgressPreference = 'SilentlyContinue'; \
    $nodeZip = Join-Path $env:TEMP ('node-v' + $env:NODE_VERSION + '-win-x64.zip'); \
    Invoke-WebRequest -Uri ('https://nodejs.org/dist/v' + $env:NODE_VERSION + '/node-v' + $env:NODE_VERSION + '-win-x64.zip') -OutFile $nodeZip; \
    Expand-Archive -LiteralPath $nodeZip -DestinationPath $env:TEMP; \
    Move-Item -LiteralPath (Join-Path $env:TEMP ('node-v' + $env:NODE_VERSION + '-win-x64')) -Destination C:\nodejs; \
    Remove-Item -LiteralPath $nodeZip; \
    npm.cmd install --global ('@openai/codex@' + $env:CODEX_VERSION); \
    $codexExe = Get-ChildItem -LiteralPath 'C:\npm-global\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor' -Filter codex.exe -Recurse | Select-Object -First 1; \
    if ($null -eq $codexExe) { throw 'The Windows Codex executable was not installed.' }; \
    New-Item -ItemType HardLink -Path C:\npm-global\codex.exe -Target $codexExe.FullName | Out-Null; \
    $codeModeHost = Get-ChildItem -LiteralPath 'C:\npm-global\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor' -Filter codex-code-mode-host.exe -Recurse | Select-Object -First 1; \
    if ($null -eq $codeModeHost) { throw 'The Codex command runner was not installed.' }; \
    New-Item -ItemType HardLink -Path C:\npm-global\codex-code-mode-host.exe -Target $codeModeHost.FullName | Out-Null; \
    codex.cmd --version

ENV PYTHONPATH="C:\app"
ENV SANCTUARY_HARNESS=codex
ENV SANCTUARY_WORKDIR="C:\workspace"
WORKDIR C:/workspace
COPY sanctuary C:/app/sanctuary

ENTRYPOINT ["python", "-m", "sanctuary.runtime"]
