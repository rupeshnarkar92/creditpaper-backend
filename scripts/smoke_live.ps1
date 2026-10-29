$h = @{"X-Tenant-ID"="1"}
$body = '{"website_url":"https://hyperiontech.co/"}'
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/onboarding/brand/analyze" -Method POST -Body $body -ContentType "application/json" -Headers $h -UseBasicParsing -TimeoutSec 120
    $j = $r.Content | ConvertFrom-Json
    Write-Output "name: $($j.brand.name)"
    Write-Output "colors: $($j.brand.colors | ConvertTo-Json -Compress)"
    Write-Output "typography: $($j.brand.typography | ConvertTo-Json -Compress)"
    Write-Output "status: $($j.status)"
} catch {
    Write-Output "FAIL: $($_.Exception.Message)"
}
