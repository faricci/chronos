# Lancia in ordine gli stadi di verifica della prova di esecuzione a taglia minima.
# NON lancia lo stadio "live". Si ferma al primo esito diverso da OK.
# La frase di conferma resta a carico tua: il programma la chiede a ogni stadio che puo' inviare ordini.
# Uso (PowerShell, dalla cartella del progetto):  .\script\probe_stages.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$py = ".\.venv\Scripts\python.exe"
$cfg = "C:\Users\faric\Documents\sviluppo\chronos-probe\config.yml"
$key = "C:\Users\faric\chronos-keys\probe.json"

Remove-Item Env:COINBASE_API_KEY, Env:COINBASE_API_SECRET -ErrorAction SilentlyContinue
$env:CHRONOS_LIVE_CONFIRM = "I_ACCEPT_REAL_ORDERS"
$env:CHRONOS_EXEC_PROBE_CONFIRM = "BTC-EUR:3.00"

$stages = @("preflight", "preview", "place-cancel", "expire", "reject", "stop-switch")
foreach ($s in $stages) {
    Write-Host ""
    Write-Host "=== Stadio: $s ===" -ForegroundColor Cyan
    if ($s -eq "preflight") {
        & $py -m modules.probe preflight --config $cfg --key-file $key
    } else {
        & $py -m modules.probe run --stage $s --config $cfg --key-file $key
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Stadio '$s' NON OK (codice $LASTEXITCODE). Mi fermo: leggi le righe [NO] e dimmelo." -ForegroundColor Red
        exit $LASTEXITCODE
    }
}
Write-Host ""
Write-Host "Tutti gli stadi di verifica sono OK. Lo stadio 'live' si lancia a parte, prima delle 09:00 UTC, quando sei presente." -ForegroundColor Green
