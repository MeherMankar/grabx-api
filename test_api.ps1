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

    Write-Host "`nQueueing asynchronous extraction..."
    $Job = Invoke-RestMethod -Method Post -Uri "$BaseUrl/jobs" `
        -Headers $Headers -ContentType "application/json" -Body $Body -TimeoutSec 30
    Write-Host "Job ID: $($Job.job_id); initial status: $($Job.status)"

    $StatusUrl = "$BaseUrl/jobs/$([uri]::EscapeDataString($Job.job_id))"
    $JobStatus = $null
    $StatusUnavailable = $false
    for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
        Start-Sleep -Seconds 3
        try {
            $JobStatus = Invoke-RestMethod -Uri $StatusUrl `
                -Headers $Headers -TimeoutSec 30
        } catch {
            $Response = $_.Exception.Response
            if ($Response) {
                $Reader = New-Object System.IO.StreamReader($Response.GetResponseStream())
                $ResponseBody = $Reader.ReadToEnd()
                $Reader.Dispose()
                Write-Warning "Job status request returned HTTP $([int]$Response.StatusCode): $ResponseBody"
            } else {
                Write-Warning "Could not reach the job status endpoint: $($_.Exception.Message)"
            }
            $StatusUnavailable = $true
            break
        }
        Write-Host "Job status: $($JobStatus.status)"
        if ($JobStatus.queue) {
            $WorkerSummary = @($JobStatus.queue.workers | ForEach-Object {
                "$($_.name) [$($_.state)]"
            }) -join ", "
            if (-not $WorkerSummary) {
                $WorkerSummary = "none registered"
            }
            Write-Host "Queue depth: $($JobStatus.queue.queued_jobs); workers: $WorkerSummary"
        }
        if ($JobStatus.status -in @("finished", "failed")) {
            break
        }
    }

    if ($JobStatus.status -eq "finished") {
        Write-Host "Job result: $($JobStatus.result.data.title)"
    } elseif ($JobStatus.status -eq "failed") {
        Write-Warning "Extraction job failed. Check the Koyeb RQ worker logs."
    } elseif ($StatusUnavailable) {
        Write-Warning "The job was queued, but its status could not be read. Check the Koyeb API logs for the /jobs/$($Job.job_id) request and the worker logs."
    } else {
        Write-Warning "Job is still queued/running after 120 seconds. Use the worker and queue details above to diagnose the Koyeb grabx-worker service."
    }
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
