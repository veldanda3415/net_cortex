[CmdletBinding()]
param(
    [ValidateSet("List", "Use", "Save", "Doctor")]
    [string]$Action = "List",

    [string]$Profile
)

$defaultAdcPath = Join-Path $env:APPDATA "gcloud\application_default_credentials.json"

$profiles = [ordered]@{
    hughes = @{
        Account = "ravi.veldanda@hughes.com"
        ProjectId = "nad-cne-nwops-phase1-lab"
        GcloudConfig = "hughes-sage"
        AdcFile = Join-Path $env:APPDATA "gcloud\adc-hughes-sage.json"
    }
    personal = @{
        Account = "veldanda3415@gmail.com"
        ProjectId = "net-cortex-dev"
        GcloudConfig = "personal-default"
        AdcFile = Join-Path $env:APPDATA "gcloud\adc-personal.json"
    }
}

function Get-ProfileConfig {
    param([string]$Name)

    if (-not $Name) {
        throw "Provide -Profile with one of: $($profiles.Keys -join ', ')"
    }

    if (-not $profiles.Contains($Name)) {
        throw "Unknown profile '$Name'. Valid values: $($profiles.Keys -join ', ')"
    }

    return $profiles[$Name]
}

function Ensure-GcloudConfig {
    param([hashtable]$ProfileConfig)

    $configName = $ProfileConfig.GcloudConfig
    $existing = gcloud config configurations list --format="value(name)" 2>$null
    if ($existing -notcontains $configName) {
        gcloud config configurations create $configName | Out-Null
    }
}

function Save-ProfileAdc {
    param([string]$Name)

    $profileConfig = Get-ProfileConfig -Name $Name

    if (-not (Test-Path $defaultAdcPath)) {
        throw "Default ADC file was not found at '$defaultAdcPath'. Run 'gcloud auth application-default login' first."
    }

    $tokenInfo = Get-TokenInfo
    if (-not $tokenInfo -or -not $tokenInfo.email) {
        throw "Could not resolve the current ADC principal. Run 'gcloud auth application-default login' and try again."
    }
    if ($tokenInfo.email -ne $profileConfig.Account) {
        throw @"
ADC principal mismatch for profile '$Name'.
Current ADC principal: $($tokenInfo.email)
Expected account:      $($profileConfig.Account)

Run:
  gcloud auth login $($profileConfig.Account)
  gcloud auth application-default login
Then run Save again.
"@
    }

    $targetDir = Split-Path -Path $profileConfig.AdcFile -Parent
    if (-not (Test-Path $targetDir)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    }

    Copy-Item -Path $defaultAdcPath -Destination $profileConfig.AdcFile -Force
    Write-Host "Saved ADC for profile '$Name' to '$($profileConfig.AdcFile)'" -ForegroundColor Green
}

function Get-TokenInfo {
    param(
        [string]$AdcPath
    )

    $previousAdc = $env:GOOGLE_APPLICATION_CREDENTIALS
    try {
        if ($AdcPath) {
            $env:GOOGLE_APPLICATION_CREDENTIALS = $AdcPath
        } elseif ($env:GOOGLE_APPLICATION_CREDENTIALS) {
            Remove-Item Env:GOOGLE_APPLICATION_CREDENTIALS -ErrorAction SilentlyContinue
        }

        $token = gcloud auth application-default print-access-token 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $token) {
            return $null
        }

        try {
            return Invoke-RestMethod -Uri "https://oauth2.googleapis.com/tokeninfo?access_token=$token" -Method Get
        } catch {
            return $null
        }
    }
    finally {
        if ($null -ne $previousAdc -and $previousAdc -ne "") {
            $env:GOOGLE_APPLICATION_CREDENTIALS = $previousAdc
        } else {
            Remove-Item Env:GOOGLE_APPLICATION_CREDENTIALS -ErrorAction SilentlyContinue
        }
    }
}

function Show-ProfileDoctor {
    Write-Host "Active gcloud configuration:" -ForegroundColor Cyan
    gcloud config configurations list --filter="is_active=true"

    Write-Host "" 
    Write-Host "Runtime environment overrides (current shell):" -ForegroundColor Cyan
    Get-ChildItem Env:ACTIVE_GCP_PROFILE,Env:GOOGLE_CLOUD_PROJECT,Env:PROJECT_ID,Env:GOOGLE_APPLICATION_CREDENTIALS -ErrorAction SilentlyContinue | Format-Table -AutoSize

    Write-Host "" 
    Write-Host "Default ADC file:" -ForegroundColor Cyan
    if (Test-Path $defaultAdcPath) {
        $adcJson = Get-Content $defaultAdcPath | ConvertFrom-Json
        Write-Host "path=$defaultAdcPath"
        Write-Host "quota_project_id=$($adcJson.quota_project_id)"
        $defaultToken = Get-TokenInfo
        if ($defaultToken) {
            Write-Host "principal=$($defaultToken.email)"
        } else {
            Write-Host "principal=unable to resolve"
        }
    } else {
        Write-Host "path=$defaultAdcPath (missing)"
    }

    Write-Host "" 
    Write-Host "Saved profile ADC files:" -ForegroundColor Cyan
    foreach ($entry in $profiles.GetEnumerator()) {
        $exists = Test-Path $entry.Value.AdcFile
        if (-not $exists) {
            Write-Host ("{0}: missing -> {1}" -f $entry.Key, $entry.Value.AdcFile)
            continue
        }

        $adcJson = Get-Content $entry.Value.AdcFile | ConvertFrom-Json
        $tokenInfo = Get-TokenInfo -AdcPath $entry.Value.AdcFile
        $principal = if ($tokenInfo) { $tokenInfo.email } else { "unable to resolve" }
        Write-Host ("{0}: principal={1}, quota_project_id={2}, file={3}" -f $entry.Key, $principal, $adcJson.quota_project_id, $entry.Value.AdcFile)
    }
}

function Use-Profile {
    param([string]$Name)

    $profileConfig = Get-ProfileConfig -Name $Name
    Ensure-GcloudConfig -ProfileConfig $profileConfig

    gcloud config configurations activate $profileConfig.GcloudConfig | Out-Null
    gcloud config set account $profileConfig.Account | Out-Null
    gcloud config set project $profileConfig.ProjectId | Out-Null

    $env:ACTIVE_GCP_PROFILE = $Name
    $env:GOOGLE_CLOUD_PROJECT = $profileConfig.ProjectId
    $env:PROJECT_ID = $profileConfig.ProjectId
    $env:GOOGLE_GENAI_USE_VERTEXAI = "True"

    if (-not (Test-Path $profileConfig.AdcFile)) {
        throw @"
Saved ADC file for '$Name' is missing: $($profileConfig.AdcFile)

Run these commands once for this profile:
  gcloud auth login $($profileConfig.Account)
  gcloud auth application-default login
  .\scripts\switch-gcp-profile.ps1 -Action Save -Profile $Name
"@
    }

    $env:GOOGLE_APPLICATION_CREDENTIALS = $profileConfig.AdcFile
    $tokenInfo = Get-TokenInfo -AdcPath $profileConfig.AdcFile

    Write-Host "Active profile: $Name" -ForegroundColor Green
    Write-Host "gcloud config: $($profileConfig.GcloudConfig)" -ForegroundColor Green
    Write-Host "project: $($profileConfig.ProjectId)" -ForegroundColor Green
    Write-Host "ADC file: $($profileConfig.AdcFile)" -ForegroundColor Green
    if ($tokenInfo) {
        Write-Host "ADC principal: $($tokenInfo.email)" -ForegroundColor Green
    } else {
        Write-Warning "Could not resolve ADC principal from tokeninfo."
    }

    Write-Host "Use the same shell for adk web . so these env vars are preserved." -ForegroundColor Yellow
    Write-Host "" 
    Write-Host "Quick verify command:" -ForegroundColor Cyan
    Write-Host "  gcloud auth application-default print-access-token | Out-Null; .\scripts\switch-gcp-profile.ps1 -Action Doctor"

}

function Show-Profiles {
    foreach ($entry in $profiles.GetEnumerator()) {
        $adcExists = Test-Path $entry.Value.AdcFile
        Write-Host ("{0,-10} account={1} project={2} config={3} adcSaved={4}" -f `
            $entry.Key, $entry.Value.Account, $entry.Value.ProjectId, $entry.Value.GcloudConfig, $adcExists)
    }
}

switch ($Action) {
    "List" { Show-Profiles }
    "Save" { Save-ProfileAdc -Name $Profile }
    "Use" { Use-Profile -Name $Profile }
    "Doctor" { Show-ProfileDoctor }
}