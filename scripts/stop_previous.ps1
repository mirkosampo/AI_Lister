# Chiamato da start.bat: se sulla porta c'è già un'istanza di AI Lister (magari di una versione precedente)
# la chiude, così parte sempre il codice aggiornato. Se l'istanza sta facendo una ricerca chiede conferma.
# Codici di uscita: 0 = porta libera, 1 = porta occupata da un altro programma, 2 = l'utente tiene l'istanza attuale.
param([int]$Port = 8765)

$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $conn) { exit 0 }

$proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)"
if ($proc.CommandLine -notmatch "app\.main:app") {
    Write-Host "La porta $Port e' occupata da un altro programma ($($proc.Name)). Chiudilo o cambia PORT in start.bat."
    exit 1
}

try {
    $scan = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/scan/status" -TimeoutSec 5
} catch { $scan = $null }

if ($scan -and $scan.running) {
    Write-Host ""
    Write-Host "AI Lister e' gia' acceso e sta facendo una ricerca ($($scan.done)/$($scan.total) video: $($scan.current))."
    $answer = Read-Host "Interromperla e riavviare con la versione aggiornata? [s/N]"
    if ($answer -notmatch '^[sSyY]') { exit 2 }
}

Write-Host "Chiudo l'istanza di AI Lister gia' attiva (avviata il $($proc.CreationDate))..."
Stop-Process -Id $proc.ProcessId -Force
for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { exit 0 }
    Start-Sleep -Milliseconds 500
}
Write-Host "La porta $Port non si e' liberata."
exit 1
