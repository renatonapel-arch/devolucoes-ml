# Sobe o backend de leitura das devoluções ML (Fase 1).
# Conferente (mobile): http://127.0.0.1:8078/   |   Gestor (desktop): http://127.0.0.1:8078/gestor
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown" } | Sort-Object SkipAsSource | Select-Object -First 1).IPAddress
Write-Host "Devolucoes ML - Fase 1 (SO LEITURA)" -ForegroundColor Green
Write-Host ("  Conferente: http://127.0.0.1:8078/        (mobile)") -ForegroundColor Cyan
Write-Host ("  Gestor:     http://127.0.0.1:8078/gestor   (desktop)") -ForegroundColor Cyan
Write-Host ("  No iPhone:  http://{0}:8078   (mesma rede Wi-Fi, se firewall liberado)" -f $ip) -ForegroundColor Cyan
python -m uvicorn backend:app --host 0.0.0.0 --port 8078
