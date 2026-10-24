$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $projectRoot
$pgBin = 'C:\Program Files\PostgreSQL\17\bin'
$privateDir = Join-Path $projectRoot 'var\private'
$clusterDir = Join-Path $privateDir 'postgres'
$pythonExe = Join-Path $projectRoot 'services\api\.venv\Scripts\python.exe'
New-Item -ItemType Directory -Force $privateDir | Out-Null
if (!(Test-Path (Join-Path $projectRoot '.env'))) {
    $passwordBytes = New-Object byte[] 32
    $projectRandom = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $projectRandom.GetBytes($passwordBytes)
    $projectRandom.Dispose()
    $projectPassword = [BitConverter]::ToString($passwordBytes).Replace('-','').ToLower()
    "DATABASE_URL=postgresql+psycopg://portfolio_owner:${projectPassword}@127.0.0.1:55432/portfolio" | Set-Content -Encoding utf8 (Join-Path $projectRoot '.env')
}
if (!(Test-Path (Join-Path $clusterDir 'PG_VERSION'))) {
    $envLine = Get-Content (Join-Path $projectRoot '.env') | Where-Object { $_ -like 'DATABASE_URL=*' }
    $connectionUri = [Uri]($envLine.Replace('DATABASE_URL=','').Replace('postgresql+psycopg://','postgresql://'))
    if ($connectionUri.Port -ne 55432 -or $connectionUri.Host -ne '127.0.0.1' -or !$connectionUri.UserInfo.StartsWith('portfolio_owner:')) { throw '已有 .env 使用其他数据库，请按 README 手动迁移，脚本不会改动它。' }
    $passwordFile = Join-Path $privateDir 'init-password.tmp'
    $connectionUri.UserInfo.Split(':',2)[1] | Set-Content -NoNewline -Encoding ascii $passwordFile
    try {
        & (Join-Path $pgBin 'initdb.exe') -D $clusterDir -U portfolio_owner --encoding=UTF8 --locale=C --auth=scram-sha-256 "--pwfile=$passwordFile"
        if ($LASTEXITCODE -ne 0) { throw 'initdb failed' }
    } finally { Remove-Item -LiteralPath $passwordFile -ErrorAction SilentlyContinue }
}
& (Join-Path $pgBin 'pg_ctl.exe') -D $clusterDir status | Out-Null
if ($LASTEXITCODE -ne 0) {
    & (Join-Path $pgBin 'pg_ctl.exe') -D $clusterDir -l (Join-Path $privateDir 'postgres.log') -o '-h 127.0.0.1 -p 55432' -w start
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL start failed' }
}
$env:PYTHONPATH = Join-Path $projectRoot 'services\api'
& $pythonExe -m app.bootstrap
if ($LASTEXITCODE -ne 0) { throw 'Database setup failed' }
& $pythonExe -m alembic -c migrations/alembic.ini upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Migration failed' }
& $pythonExe -m app.bootstrap --seed
if ($LASTEXITCODE -ne 0) { throw 'Seed failed' }
Write-Host 'Local PostgreSQL ready on 127.0.0.1:55432. Open the VS Code workspace and run the platform task.'
