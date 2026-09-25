<#
    API_call.ps1

    Plex API functions. Dot-source this file, then call
    Get-RemoteMovieTitles - don't run this file directly, it no longer does
    anything on its own:

        . .\API_call.ps1
        $titles = Get-RemoteMovieTitles -AccountToken $Config.PlexAccountToken `
            -ClientIdentifier $Config.PlexClientIdentifier `
            -FriendServerName $Config.FriendServerName `
            -RemoteMoviesFile $Config.RemoteMoviesFile
#>

. "$PSScriptRoot\MovieSearchHelpers.ps1"

function Get-RemoteMovieTitles {
    <#
        Returns a set of cleaned/normalized movie titles available on a
        remote (e.g. a friend's) Plex server, for use as a fallback lookup
        once a local search has already come up empty for both the exact
        and cleaned title. Tries a live check via the Plex API first; if
        that fails for any reason - the server's offline, the network's
        down, a token expired - falls back to the last cached
        RemoteMoviesFile on disk instead, so a temporary outage on their
        end doesn't break your whole run.
    #>
    param(
        [Parameter(Mandatory)][string]$AccountToken,
        [Parameter(Mandatory)][string]$ClientIdentifier,
        [Parameter(Mandatory)][string]$FriendServerName,
        [Parameter(Mandatory)][string]$RemoteMoviesFile
    )

    try {
        $resources = Invoke-RestMethod -Uri "https://plex.tv/api/v2/resources?X-Plex-Client-Identifier=$ClientIdentifier&X-Plex-Token=$AccountToken&includeHttps=1" -Method Get -Headers @{ 'Accept' = 'application/json' }

        $friendServer = $resources | Where-Object { $_.name -eq $FriendServerName -and $_.product -eq 'Plex Media Server' }
        if (-not $friendServer) {
            throw "No server named '$FriendServerName' was found."
        }

        # Prefer a direct, non-relay, non-local connection - local only
        # matters if you're on their network, which you're not.
        $connection = $friendServer.connections | Where-Object { -not $_.relay -and -not $_.local } | Select-Object -First 1
        if (-not $connection) {
            $connection = $friendServer.connections | Select-Object -First 1   # nothing direct - fall back to relay
        }

        $remoteUri   = $connection.uri
        $remoteToken = $friendServer.accessToken

        $remoteSections = Invoke-RestMethod -Uri "$remoteUri/library/sections/?X-Plex-Token=$remoteToken" -Method Get -Headers @{ 'Accept' = 'application/json' }
        #$remoteMovieKey = ($remoteSections.MediaContainer.Directory | Where-Object { $_.type -eq 'movie' }).key
        $remoteMovies   = (Invoke-RestMethod -Uri "$remoteUri/library/sections/1/all?X-Plex-Token=$remoteToken" -Method Get -Headers @{ 'Accept' = 'application/json' }).MediaContainer.Metadata

        # Dotting straight into .title on the array pulls that property out
        # of every element at once, as a flat string array - no
        # Select-Object needed.
        $titles = $remoteMovies.title

        # Refresh the cache for next time, so a future outage still has
        # something recent to fall back to.
        $titles | Out-File -FilePath $RemoteMoviesFile -Encoding utf8

        Write-Host "Live-checked '$FriendServerName': $($titles.Count) movies."
    }
    catch {
        Write-Warning "Live check of '$FriendServerName' failed ($($_.Exception.Message)) - falling back to the cached list."

        if (-not (Test-Path $RemoteMoviesFile)) {
            Write-Warning "No cached file found at $RemoteMoviesFile either - remote matching will be skipped this run."
            return [System.Collections.Generic.HashSet[string]]::new()
        }

        $titles = Get-Content -Path $RemoteMoviesFile
    }

    $normalized = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($title in $titles) {
        [void]$normalized.Add((Get-SearchFriendlyTitle -Title $title))
    }

    return $normalized
}
