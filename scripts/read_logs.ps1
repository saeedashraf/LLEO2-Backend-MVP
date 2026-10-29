param([string]$Filter = "Error downloading", [int]$Limit = 10, [string]$Fresh = "1h")
$q = 'resource.type="cloud_run_revision" AND resource.labels.service_name="ndvi6-analysis-api" AND textPayload:"' + $Filter + '"'
gcloud logging read $q --limit $Limit --freshness $Fresh --format="value(timestamp,textPayload)" 2>&1 | Out-File "$env:TEMP\lleo_logs.txt" -Encoding utf8
Get-Content "$env:TEMP\lleo_logs.txt"
