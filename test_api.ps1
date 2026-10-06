param(
    [string]$BaseUrl = "https://nearby-sherline-mehermankarofficial-3b587d66.koyeb.app",
    [string]$VideoUrl = "https://xhamster19.com/videos/indian-stepmom-very-hard-fucking-doggy-style-viral-hindi-sex-video-xhstHAM"
)

$ErrorActionPreference = "Stop"
$BaseUrl = $BaseUrl.TrimEnd("/")
$SecureKey = Read-Host "Koyeb API_KEY" -AsSecureString
$KeyPointer = [IntPtr]::Zero
$ApiKey = $null

try {
    $KeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)
    $ApiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($KeyPointer)
    $Headers = @{ "X-API-Key" = $ApiKey }
    $Body = @{ url = $VideoUrl } | ConvertTo-Json

    Write-Host "`nChecking API and Redis health..."
    $Health = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 30
    Write-Host "API: $($Health.status); Redis: $($Health.redis); Cache: $($Health.cache.backend)"
    if ($Health.status -ne "ok") {
        throw "API health check did not return status 'ok'."
    }

    Write-Host "`nTesting XHamster extraction..."
    $Extract = Invoke-RestMethod -Method Post -Uri "$BaseUrl/xh/download" `
        -Headers $Headers -ContentType "application/json" -Body $Body -TimeoutSec 120
    Write-Host "Extracted: $($Extract.data.title)"
    $Extract.data.qualities | Select-Object quality, format | Format-Table -AutoSize

    $WatchUrl = "$BaseUrl/xh/watch?url=$([uri]::EscapeDataString($VideoUrl))"
    Write-Host "Opening player: $WatchUrl"
    Start-Process $WatchUrl

    Write-Host "`nAll tests passed successfully."
} catch {
    Write-Error "API test failed: $($_.Exception.Message)"
    exit 1
} finally {
    if ($KeyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($KeyPointer)
    }
    $ApiKey = $null
    $Headers = $null
    $SecureKey = $null
}
