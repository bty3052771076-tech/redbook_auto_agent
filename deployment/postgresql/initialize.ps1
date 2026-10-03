$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
if ([IO.Path]::GetPathRoot($root) -ne 'E:\') { throw 'This deployment must remain on E:.' }
$bin = Join-Path $PSScriptRoot '18.6/pgsql/bin'
$private = Join-Path $root 'data/knowledge/postgresql-local'
$data = Join-Path $private 'pgdata'
$credentialFile = Join-Path $private 'connection.json'
$backup = Join-Path $root 'data/knowledge/backups/postgresql'
$log = Join-Path $root 'data/logs/postgresql'
$env:TEMP = Join-Path $root 'data/tmp/postgresql'
$env:TMP = $env:TEMP
$env:PGCLIENTENCODING = 'UTF8'
$env:PGCONNECT_TIMEOUT = '5'
$OutputEncoding = New-Object Text.UTF8Encoding($false)
$utf8 = New-Object Text.UTF8Encoding($false)
if (Test-Path $credentialFile) { throw 'Credentials already exist; initialization will not overwrite them.' }
if (Test-Path $data) { throw 'Data directory already exists; initialization refuses to overwrite it.' }
if (Get-NetTCPConnection -State Listen -LocalPort 5433 -ErrorAction SilentlyContinue) { throw 'Port 5433 is occupied.' }

function Protect-Directory([string]$path) {
    New-Item -ItemType Directory -Path $path -Force | Out-Null
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().User
    foreach ($sid in @($user, (New-Object Security.Principal.SecurityIdentifier('S-1-5-18')))) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $path -AclObject $acl
}
foreach ($path in @($private, $backup, $log, $env:TEMP)) { Protect-Directory $path }

function New-Password {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
}
$connection = [ordered]@{
    host = '127.0.0.1'; port = 5433; database = 'redbook_knowledge'
    admin_user = 'redbook_admin'; admin_password = (New-Password)
    migration_user = 'redbook_migrator'; migration_password = (New-Password)
    app_user = 'redbook_app'; app_password = (New-Password)
}
[IO.File]::WriteAllText($credentialFile, ($connection | ConvertTo-Json), $utf8)
$pwfile = Join-Path $private 'init-password.tmp'
try {
    [IO.File]::WriteAllText($pwfile, $connection.admin_password, $utf8)
    & "$bin/initdb.exe" -D $data -U $connection.admin_user --encoding=UTF8 --locale=C --text-search-config=simple --auth=scram-sha-256 --data-checksums "--pwfile=$pwfile"
    if ($LASTEXITCODE -ne 0) { throw 'initdb failed; existing credentials retained for investigation.' }
} finally {
    if (Test-Path -LiteralPath $pwfile) { Remove-Item -LiteralPath $pwfile }
}

$logPath = $log.Replace('\', '/')
$config = @"
# Local-only PostgreSQL deployment. All paths are on E:.
listen_addresses = '127.0.0.1'
port = 5433
max_connections = 40
shared_buffers = '128MB'
work_mem = '4MB'
maintenance_work_mem = '64MB'
effective_cache_size = '512MB'
max_wal_size = '1GB'
min_wal_size = '80MB'
fsync = on
full_page_writes = on
synchronous_commit = on
password_encryption = 'scram-sha-256'
timezone = 'Asia/Shanghai'
log_timezone = 'Asia/Shanghai'
logging_collector = on
log_destination = 'stderr'
log_directory = '$logPath'
log_filename = 'postgresql-%Y-%m-%d_%H%M%S.log'
log_rotation_age = '1d'
log_rotation_size = '10MB'
log_statement = 'none'
log_min_error_statement = 'panic'
log_parameter_max_length_on_error = 0
log_line_prefix = '%m [%p] %u@%d '
"@
[IO.File]::WriteAllText((Join-Path $data 'local.conf'), $config, $utf8)
[IO.File]::AppendAllText((Join-Path $data 'postgresql.conf'), "`ninclude = 'local.conf'`n", $utf8)
$hba = @"
# Only this application's roles are allowed, using SCRAM passwords.
host all redbook_admin 127.0.0.1/32 scram-sha-256
host redbook_knowledge redbook_migrator,redbook_app 127.0.0.1/32 scram-sha-256
host all all 0.0.0.0/0 reject
host all all ::0/0 reject
"@
[IO.File]::WriteAllText((Join-Path $data 'pg_hba.conf'), $hba, $utf8)
$start = Start-Process -FilePath "$bin/pg_ctl.exe" -ArgumentList @('start', '-D', "`"$data`"", '-l', "`"$log/bootstrap.log`"", '-w', '-t', '30') -WindowStyle Hidden -PassThru
$deadline = [DateTime]::UtcNow.AddSeconds(30)
$ready = $false
while ([DateTime]::UtcNow -lt $deadline) {
    & "$bin/pg_ctl.exe" status -D $data *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    $start.Refresh()
    if ($start.HasExited -and $start.ExitCode -ne 0) { break }
    Start-Sleep -Milliseconds 250
}
if (!$ready) { throw "PostgreSQL did not start. Inspect $log/bootstrap.log" }

$env:PGPASSWORD = $connection.admin_password
$bootstrapSql = Join-Path $private 'bootstrap.sql'
try {
    $sql = @"
CREATE ROLE redbook_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE redbook_migrator LOGIN PASSWORD '$($connection.migration_password)' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 3;
GRANT redbook_owner TO redbook_migrator;
CREATE ROLE redbook_app LOGIN PASSWORD '$($connection.app_password)' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 12;
CREATE DATABASE redbook_knowledge OWNER redbook_owner ENCODING 'UTF8';
REVOKE ALL ON DATABASE redbook_knowledge FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE redbook_knowledge TO redbook_app;
GRANT CONNECT ON DATABASE redbook_knowledge TO redbook_migrator;
ALTER ROLE redbook_app SET search_path = knowledge, public;
ALTER ROLE redbook_app SET statement_timeout = '30s';
ALTER ROLE redbook_app SET lock_timeout = '5s';
ALTER ROLE redbook_app SET idle_in_transaction_session_timeout = '60s';
"@
    [IO.File]::WriteAllText($bootstrapSql, $sql, $utf8)
    & "$bin/psql.exe" -X -w -h 127.0.0.1 -p 5433 -U redbook_admin -d postgres -v ON_ERROR_STOP=1 -f $bootstrapSql
    if ($LASTEXITCODE -ne 0) { throw 'Database/role initialization failed; no automatic reinitialization was attempted.' }
    Remove-Item -LiteralPath $bootstrapSql -Force
    $sql = @'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO redbook_app, redbook_owner;
CREATE EXTENSION pg_trgm WITH SCHEMA public;
CREATE SCHEMA knowledge AUTHORIZATION redbook_owner;
GRANT USAGE ON SCHEMA knowledge TO redbook_app;
ALTER DEFAULT PRIVILEGES FOR ROLE redbook_owner IN SCHEMA knowledge GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO redbook_app;
ALTER DEFAULT PRIVILEGES FOR ROLE redbook_owner IN SCHEMA knowledge GRANT USAGE, SELECT ON SEQUENCES TO redbook_app;
ALTER DEFAULT PRIVILEGES FOR ROLE redbook_owner REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
'@
    if (Test-Path (Join-Path $bin '../share/extension/vector.control')) { $sql += "`nCREATE EXTENSION vector;`n" }
    $schemaSql = Join-Path $private 'schema.sql'
    [IO.File]::WriteAllText($schemaSql, $sql, $utf8)
    & "$bin/psql.exe" -X -w -h 127.0.0.1 -p 5433 -U redbook_admin -d redbook_knowledge -v ON_ERROR_STOP=1 -f $schemaSql
    if ($LASTEXITCODE -ne 0) { throw 'Schema initialization failed.' }
} finally {
    Remove-Item -LiteralPath $bootstrapSql -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $private 'schema.sql') -Force -ErrorAction SilentlyContinue
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    $sql = $null
    $connection = $null
}
Write-Host 'Initialized redbook_knowledge. Credentials are local-only; no application data has been migrated.'
