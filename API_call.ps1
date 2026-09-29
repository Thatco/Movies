<#
    API_call.ps1

    Plex API functions. Dot-source this file, then call
    Get-RemoteMovieAvailability - don't run this file directly, it no
    longer does anything on its own:

        . .\API_call.ps1
        $availability = Get-RemoteMovieAvailability -AccountToken $Config.PlexAccountToken `
            -ClientIdentifier $Config.PlexClientIdentifier `
            -FriendServerNames $Config.FriendServerNames `
            -RemoteMoviesFolder $Config.RemoteMoviesFolder `
            -LibraryDefaultsFile $Config.LibraryDefaultsFile
#>

. "$PSScriptRoot\MovieSearchHelpers.ps1"

function Get-LibraryDefaults {
    <# Reads the saved server-name -> library-key map, or an empty
       hashtable if nothing's been saved yet. #>
    param([Parameter(Mandatory)][string]$Path)

    if (Test-Path $Path) {
        return Import-PowerShellDataFile -Path $Path
    }
    return @{}
}

function Save-LibraryDefaults {
    <# PowerShell has Import-PowerShellDataFile built in, but no matching
       Export- counterpart, so this hand-writes the .psd1 text - same
       format Config.local.psd1 already uses, just a flat string -> string
       map instead of the bigger settings hashtable. #>
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][hashtable]$Defaults
    )

    $folder = Split-Path -Path $Path -Parent
    if ($folder -and -not (Test-Path $folder)) {
        New-Item -ItemType Directory -Path $folder -Force | Out-Null
    }

    $lines = foreach ($key in $Defaults.Keys) {
        "    '$key' = '$($Defaults[$key])'"
    }
    "@{`n$($lines -join "`n")`n}" | Out-File -FilePath $Path -Encoding utf8
}

function Get-RemoteMovieTitlesForServer {
    <#
        Returns cleaned/normalized movie titles available on ONE remote
        Plex server. Tries a live check via the Plex API first; if that
        fails for any reason - the server's offline, the network's down,
        a token expired - falls back to the last cached RemoteMoviesFile
        on disk instead, so a temporary outage on their end doesn't break
        the whole run.

        If the server has more than one movie library, the choice is
        asked once and then remembered in LibraryDefaultsFile (keyed by
        server name) so future runs - for this server or any other in
        FriendServerNames - don't ask again.
    #>
    param(
        [Parameter(Mandatory)][string]$AccountToken,
        [Parameter(Mandatory)][string]$ClientIdentifier,
        [Parameter(Mandatory)][string]$FriendServerName,
        [Parameter(Mandatory)][string]$RemoteMoviesFile,
        [Parameter(Mandatory)][string]$LibraryDefaultsFile
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

        # Kept from an earlier experiment - restates values already passed
        # in as parameters, nothing reads these back yet.
        $ConfigTokenFile = Join-Path -Path $PSScriptRoot -ChildPath 'Config Text Files\ConfigToken.txt'
        $RemoteTokenFile = Join-Path -Path $PSScriptRoot -ChildPath 'Config Text Files\RemoteToken.txt'
        $configToken = @"
PlexAccountToken       = '$AccountToken'
PlexClientIdentifier   = '$ClientIdentifier'
"@
        $configToken | Out-File -FilePath $ConfigTokenFile -Encoding utf8
        $remoteToken | Out-File -FilePath $RemoteTokenFile -Encoding utf8

        $remoteSections = Invoke-RestMethod -Uri "$remoteUri/library/sections/?X-Plex-Token=$remoteToken" -Method Get -Headers @{ 'Accept' = 'application/json' }
        $movieLibraries = @($remoteSections.MediaContainer.Directory | Where-Object { $_.type -eq 'movie' })

        if ($movieLibraries.Count -eq 0) {
            throw "No movie library was found on '$FriendServerName'."
        }

        # A previously-saved choice for THIS server wins, if one exists
        # and still matches a library that's actually there.
        $libraryDefaults = Get-LibraryDefaults -Path $LibraryDefaultsFile
        $savedKey     = $libraryDefaults[$FriendServerName]
        $savedLibrary = $movieLibraries | Where-Object { $_.key -eq $savedKey } | Select-Object -First 1

        if ($savedLibrary) {
            $remoteMovieKey = $savedLibrary.key
        }
        elseif ($movieLibraries.Count -eq 1) {
            $remoteMovieKey = $movieLibraries[0].key
        }
        else {
            do {
                Write-Host "Multiple movie libraries were found on '$FriendServerName':"
                for ($j = 0; $j -lt $movieLibraries.Count; $j++) {
                    Write-Host "$($j + 1): $($movieLibraries[$j].title)"
                }
                $choice = Read-Host "Please enter the number of the library to use (this will be remembered next time)"
            } while ($choice -lt 1 -or $choice -gt $movieLibraries.Count)

            $remoteMovieKey = $movieLibraries[$choice - 1].key

            $libraryDefaults[$FriendServerName] = $remoteMovieKey
            Save-LibraryDefaults -Path $LibraryDefaultsFile -Defaults $libraryDefaults
        }

        $remoteMovies = (Invoke-RestMethod -Uri "$remoteUri/library/sections/$remoteMovieKey/all?X-Plex-Token=$remoteToken" -Method Get -Headers @{ 'Accept' = 'application/json' }).MediaContainer.Metadata

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
            Write-Warning "No cached file found at $RemoteMoviesFile either - '$FriendServerName' will be skipped this run."
            return @()
        }

        $titles = Get-Content -Path $RemoteMoviesFile
    }

    return $titles | ForEach-Object { Get-SearchFriendlyTitle -Title $_ }
}

function Get-RemoteMovieAvailability {
    <#
        Checks every server in FriendServerNames and returns a hashtable
        mapping each normalized movie title to the LIST of friend server
        names it's available on (so "found on 2 of your 3 friends'
        servers" is answerable, not just "found on at least one").

        Each server is checked independently - one being unreachable
        (falling back to its own cache, or being skipped entirely if even
        that's missing) doesn't stop the others from being checked.
    #>
    param(
        [Parameter(Mandatory)][string]$AccountToken,
        [Parameter(Mandatory)][string]$ClientIdentifier,
        [Parameter(Mandatory)][string[]]$FriendServerNames,
        [Parameter(Mandatory)][string]$RemoteMoviesFolder,
        [Parameter(Mandatory)][string]$LibraryDefaultsFile
    )

    if (-not (Test-Path $RemoteMoviesFolder)) {
        New-Item -ItemType Directory -Path $RemoteMoviesFolder -Force | Out-Null
    }

    $availability = @{}

    foreach ($serverName in $FriendServerNames) {
        # One cache file per server, named after it - this is also why a
        # single RemoteMoviesFile setting became a RemoteMoviesFolder one.
        $cacheFile = Join-Path -Path $RemoteMoviesFolder -ChildPath "$serverName.txt"

        $titles = Get-RemoteMovieTitlesForServer -AccountToken $AccountToken `
            -ClientIdentifier $ClientIdentifier `
            -FriendServerName $serverName `
            -RemoteMoviesFile $cacheFile `
            -LibraryDefaultsFile $LibraryDefaultsFile

        foreach ($title in $titles) {
            if (-not $availability.ContainsKey($title)) {
                $availability[$title] = [System.Collections.Generic.List[string]]::new()
            }
            $availability[$title].Add($serverName)
        }
    }

    return $availability
}