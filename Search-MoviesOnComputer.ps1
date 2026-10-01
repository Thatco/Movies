<#
    Search-MoviesOnComputer.ps1

    Regenerates a movie list from your Letterboxd export (via
    movie_list_export.py), then searches for each title on disk using
    the "Everything" search tool (Search-Everything module), logging
    results to a text file.

    Run it interactively (it'll prompt for a mode), or pass -ShowMode
    directly for scripted/scheduled runs:

        .\Search-MoviesOnComputer.ps1 -ShowMode Both
#>

[CmdletBinding()]
param(
    [ValidateSet('ResultsOnly', 'NoResultsOnly', 'Both', 'NoLocalAndRemoteResultsOnly')]
    [string]$ShowMode
)

# ---------------------------------------------------------------------------
# Configuration - personal paths live in Config.local.psd1 (gitignored),
# so this script has nothing private baked into it and is safe to make
# public on its own. See Config.example.psd1 for the expected shape.
# ---------------------------------------------------------------------------
$ConfigPath = Join-Path $PSScriptRoot 'Config.local.psd1'
if (-not (Test-Path $ConfigPath)) {
    throw "Missing $ConfigPath. Copy Config.example.psd1 to Config.local.psd1 and fill in your own paths."
}
$Config = Import-PowerShellDataFile -Path $ConfigPath

. "$PSScriptRoot\MovieSearchHelpers.ps1"
. "$PSScriptRoot\API_call.ps1"

function Test-ConfidentMatch {
    <#
        A result is "confident" when its filename contains both the
        movie's release year AND a resolution tag (480p/720p/1080p/2160p/
        4K) - title text alone is what short titles like "M" or "Pi"
        abuse to produce false positives, but a year + resolution
        combination is much harder to match by accident.

        This is a floor, not a ceiling: plenty of correctly-named rips
        won't have a resolution tag at all, so a FALSE result here just
        means "unverified," not "wrong."
    #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string]$Year
    )

    if (-not $Year) {
        return $false
    }

    $hasYear       = $FilePath -match [regex]::Escape($Year)
    $hasResolution = $FilePath -match '(?i)\b(480p|720p|1080p|2160p|4k)\b'
    return $hasYear -and $hasResolution
}

# ---------------------------------------------------------------------------
# Ask which results to include, and which file that corresponds to
# (skipped entirely if -ShowMode was passed on the command line)
# ---------------------------------------------------------------------------
if (-not $ShowMode) {
    Write-Host "This script searches for movies on your computer and logs the results."
    Write-Host "  1) Movies with results"
    Write-Host "  2) Movies with no results"
    Write-Host "  3) Both"
    Write-Host "  4) Movies not on your computer AND not on $(Format-ServerNameList -Names $Config.FriendServerNames -Conjunction 'or') Plex server"

    do {
        $answer = Read-Host "Choose an option (1-4)"
    } while ($answer -notin '1', '2', '3', '4')

    $ShowMode = switch ($answer) {
        '1' { 'ResultsOnly' }
        '2' { 'NoResultsOnly' }
        '3' { 'Both' }
        '4' { 'NoLocalAndRemoteResultsOnly' }
    }
}

$OutputFile = switch ($ShowMode) {
    'ResultsOnly'                 { $Config.MovieResultsFile }
    'NoResultsOnly'               { $Config.MovieWithNoResultsFile }
    'Both'                        { $Config.AllMoviesFile }
    'NoLocalAndRemoteResultsOnly' { $Config.NoResultsNotOnFriendsServerFile }
}

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
# Out-File overwrites by default (no -Append below), so this isn't
# strictly required - it's just insurance against stale content if a
# previous run ever left the file in a weird state.
if (Test-Path $OutputFile) {
    Remove-Item -Path $OutputFile
}

Set-Location $Config.WorkingDir

# Regenerate the movie list JSON from the latest Letterboxd export.
& $Config.PythonExe $Config.PythonScript

if (-not (Test-Path $Config.MovieListJson)) {
    throw "Movie list JSON not found at $($Config.MovieListJson). Did the Python script run correctly?"
}

# Each entry is now {Title, Year} rather than a bare string, so the
# confidence check below has a year to work with.
$movieEntries = Get-Content -Path $Config.MovieListJson -Raw | ConvertFrom-Json

$MoviesWithNoResults = 0
$MoviesWithResults   = 0
$MoviesOnlyOnFriendsServer          = 0
$MoviesWithNoLocalAndRemoteResults  = 0

# Buffer every line in memory and write the file once at the end, instead
# of opening/closing the output file on every single Out-File -Append call.
$resultsBuffer = [System.Collections.Generic.List[string]]::new()

# $movieEntries already came from the JSON we just parsed, so the total is
# just its length - no need to hardcode a count or call back into Python.
$totalMovies = @($movieEntries).Count
$i = 0

# Get-SearchFriendlyTitle and Format-ServerNameList live in
# MovieSearchHelpers.ps1, dot-sourced above.

# $remoteCheckRan is reused below to decide whether the summary's
# friend's-server numbers actually mean anything for this run. Runs for
# every mode, ResultsOnly included - even a movie found locally is worth
# cross-referencing, since a local "match" can itself be a false
# positive, and remote availability is a useful data point either way.
$remoteCheckRan = [bool]($Config.PlexAccountToken -and $Config.FriendServerNames -and $Config.FriendServerNames.Count -gt 0)

if ($remoteCheckRan) {
    # A hashtable: normalized title -> list of friend server names that
    # have it, aggregated across every server in FriendServerNames.
    $remoteAvailability = Get-RemoteMovieAvailability -AccountToken $Config.PlexAccountToken `
        -ClientIdentifier $Config.PlexClientIdentifier `
        -FriendServerNames $Config.FriendServerNames `
        -RemoteMoviesFolder $Config.RemoteMoviesFolder `
        -LibraryDefaultsFile $Config.LibraryDefaultsFile
}
else {
    $remoteAvailability = @{}
}

# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
foreach ($movieEntry in $movieEntries) {
    $i++

    # Only redraw every 5 movies (or on the last one). Write-Progress on
    # every single iteration is what was making the terminal feel like it
    # was hanging - the script was running fine, the redraw was just slow.
    if ($i % 5 -eq 0 -or $i -eq $totalMovies) {
        Write-Progress -Activity "Creating Results List" `
            -Status "Processed $i of $totalMovies movies" `
            -PercentComplete (($i / $totalMovies) * 100)
    }

    $movie = $movieEntry.Title
    $year  = $movieEntry.Year

    # -MatchWholeWord requires each search term to match a standalone
    # word/token in the filename, not just appear as a substring - this
    # is what keeps "M" from matching "Madness.mkv".
    $searchResults = Search-Everything -Global -Filter $movie -Extension $Config.Extensions -MatchWholeWord
    $matchedTitle  = $movie

    # Exact title came up empty - retry once with punctuation stripped
    # out, since that's usually why a file that's clearly there doesn't
    # match.
    if ($null -eq $searchResults) {
        $cleanTitle = Get-SearchFriendlyTitle -Title $movie
        if ($cleanTitle -and $cleanTitle -ne $movie) {
            $searchResults = Search-Everything -Global -Filter $cleanTitle -Extension $Config.Extensions -MatchWholeWord
            if ($searchResults) {
                $matchedTitle = $cleanTitle
            }
        }
    }

    # Still nothing, and the title itself contains an alternate-title
    # separator (e.g. "La battaglia di Algeri / The Battle of Algiers") -
    # try each side on its own, since your local file is likely named
    # after only one of them. This only helps when Letterboxd's own title
    # text actually contains both forms; a single foreign-language title
    # with no English text in it at all isn't something string matching
    # can solve.
    if ($null -eq $searchResults -and $movie -match ' / ') {
        foreach ($titlePart in ($movie -split ' / ')) {
            $titlePart = $titlePart.Trim()
            $cleanPart = Get-SearchFriendlyTitle -Title $titlePart
            if (-not $cleanPart) {
                continue
            }
            $searchResults = Search-Everything -Global -Filter $cleanPart -Extension $Config.Extensions -MatchWholeWord
            if ($searchResults) {
                $matchedTitle = $titlePart
                break
            }
        }
    }

    $foundLocally = $null -ne $searchResults

    # Computed fresh every single iteration, regardless of $foundLocally,
    # so a movie that WAS found locally can never inherit a stale value
    # left over from a different, earlier movie's iteration.
    $normalizedTitle = Get-SearchFriendlyTitle -Title $movie
    $serverNames     = $remoteAvailability[$normalizedTitle]
    $onFriendsServer = $serverNames -and $serverNames.Count -gt 0

    if ($foundLocally) {
        $MoviesWithResults++
    }
    else {
        $MoviesWithNoResults++
        if ($onFriendsServer) {
            $MoviesOnlyOnFriendsServer++
        }
        else {
            $MoviesWithNoLocalAndRemoteResults++
        }
    }

    # Each mode below is an independent check against the same two
    # booleans - none of them live nested inside another mode's branch,
    # so every mode always sees the exact same, always-current state.
    if ($foundLocally -and $ShowMode -in 'ResultsOnly', 'Both') {
        $resultsToShow     = @($searchResults | Select-Object -Last $Config.MaxResultsShown)
        $confidentResults  = @($resultsToShow | Where-Object { Test-ConfidentMatch -FilePath $_ -Year $year })
        $resultCount       = @($searchResults).Count
        $resultWord        = if ($resultCount -eq 1) { 'result' } else { 'results' }

        $line = "'$movie' returned $resultCount $resultWord."
        if ($confidentResults.Count -gt 0) {
            $line += " (confident match - title, year & resolution all present)"
        }
        else {
            $line += " (low confidence - verify manually)"
        }
        if ($matchedTitle -ne $movie) {
            $line += " (matched via cleaned title: '$matchedTitle')"
        }
        if ($onFriendsServer) {
            $line += " (available on $(Format-ServerNameList -Names $serverNames) Plex server)"
        }

        $resultsBuffer.Add($line)
        $resultsBuffer.Add(('-' * 68))
        foreach ($result in $resultsToShow) {
            if ($confidentResults -contains $result) {
                $resultsBuffer.Add("  * $result")
            }
            else {
                $resultsBuffer.Add("  $result")
            }
        }
        $resultsBuffer.Add('')
    }

    if (-not $foundLocally -and $ShowMode -in 'NoResultsOnly', 'Both') {
        $line = "'$movie' did not return any results."
        if ($onFriendsServer) {
            $line += " (available on $(Format-ServerNameList -Names $serverNames) Plex server)"
        }
        $resultsBuffer.Add($line)
        $resultsBuffer.Add('')
    }

    if (-not $foundLocally -and -not $onFriendsServer -and $ShowMode -eq 'NoLocalAndRemoteResultsOnly') {
        $resultsBuffer.Add("'$movie' was not found on your computer and is also not available on $(Format-ServerNameList -Names $Config.FriendServerNames -Conjunction 'or') Plex server.")
        $resultsBuffer.Add('')
    }
}

Write-Progress -Activity "Creating Results List" -Completed

$resultsBuffer | Out-File -FilePath $OutputFile -Encoding utf8 -Width 200

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host "$(Split-Path $OutputFile -Leaf) updated at $OutputFile"
Write-Host "There are $MoviesWithResults of $totalMovies movies with results."
Write-Host "There are $MoviesWithNoResults of $totalMovies movies with no results."
if ($remoteCheckRan) {
    $serverList = Format-ServerNameList -Names $Config.FriendServerNames
    Write-Host "  - $MoviesOnlyOnFriendsServer of $totalMovies movies are available on $serverList Plex server."
    Write-Host "  - $MoviesWithNoLocalAndRemoteResults of $totalMovies movies are not on $serverList Plex server either."
}