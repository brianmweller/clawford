# Claudius Minimus launcher — loads .env and starts the relay bot.
# Called by Windows Task Scheduler on logon.

$projectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$envFile = Join-Path $projectDir ".env"

# Parse .env and set environment variables
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.+)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
        }
    }
}

# Start the bot
$python = "C:\Users\Sam\AppData\Local\Programs\Python\Python312\pythonw.exe"
$botScript = Join-Path $projectDir "telegram-relay\bot.py"

& $python $botScript --cwd $projectDir
