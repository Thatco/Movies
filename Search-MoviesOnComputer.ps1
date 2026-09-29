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

# ConvertFrom-Json handles apostrophes/quotes/commas in titles natively -
# no more regex title-extraction needed.
$movieTitles = Get-Content -Path $Config.MovieListJson -Raw | ConvertFrom-Json

$MoviesWithNoResults = 0
$MoviesWithResults   = 0
$MoviesOnlyOnFriendsServer          = 0
$MoviesWithNoLocalAndRemoteResults  = 0

# Buffer every line in memory and write the file once at the end, instead
# of opening/closing the output file on every single Out-File -Append call.
$resultsBuffer = [System.Collections.Generic.List[string]]::new()

# $movieTitles already came from the JSON we just parsed, so the total is
# just its length - no need to hardcode a count or call back into Python.
$totalMovies = @($movieTitles).Count
$i = 0

# Get-SearchFriendlyTitle now lives in MovieSearchHelpers.ps1, dot-sourced
# above, since API_call.ps1 needs the same normalization on Plex titles.

# Skip the remote check entirely for ResultsOnly - it's irrelevant to that
# mode, and this saves API calls when you don't need them. $remoteCheckRan
# is reused below to decide whether the summary's friend's-server numbers
# actually mean anything for this run.
$remoteCheckRan = [bool]($Config.PlexAccountToken -and $Config.FriendServerNames -and $Config.FriendServerNames.Count -gt 0 -and $ShowMode -ne 'ResultsOnly')

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
foreach ($movie in $movieTitles) {
    $i++

    # Only redraw every 5 movies (or on the last one). Write-Progress on
    # every single iteration is what was making the terminal feel like it
    # was hanging - the script was running fine, the redraw was just slow.
    if ($i % 5 -eq 0 -or $i -eq $totalMovies) {
        Write-Progress -Activity "Creating Results List" `
            -Status "Processed $i of $totalMovies movies" `
            -PercentComplete (($i / $totalMovies) * 100)
    }

    $searchResults = Search-Everything -Global -Filter $movie -Extension $Config.Extensions
    $matchedTitle  = $movie

    # Exact title came up empty - retry once with punctuation stripped out,
    # since that's usually why a file that's clearly there doesn't match.
    if ($null -eq $searchResults) {
        $cleanTitle = Get-SearchFriendlyTitle -Title $movie
        if ($cleanTitle -and $cleanTitle -ne $movie) {
            $searchResults = Search-Everything -Global -Filter $cleanTitle -Extension $Config.Extensions
            if ($searchResults) {
                $matchedTitle = $cleanTitle
            }
        }
    }

    $foundLocally = $null -ne $searchResults

    # Computed fresh every single iteration, regardless of $foundLocally.
    # This is what was actually going wrong before: when this was only
    # set inside the "not found" branch, a movie that WAS found locally
    # skipped the assignment and silently inherited whatever value was
    # left over from a completely different, earlier movie's iteration.
    $serverNames     = $remoteAvailability[(Get-SearchFriendlyTitle -Title $movie)]
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
        $resultCount = @($searchResults).Count
        $resultWord  = if ($resultCount -eq 1) { 'result' } else { 'results' }
        $line = "'$movie' returned $resultCount $resultWord."
        if ($matchedTitle -ne $movie) {
            $line += " (matched via cleaned title: '$matchedTitle')"
        }
        if ($onFriendsServer) {
            $line += " (available on $(Format-ServerNameList -Names $serverNames) Plex server)"
        }

        $resultsBuffer.Add($line)
        $resultsBuffer.Add(('-' * 68))
        $resultsBuffer.AddRange([string[]]($searchResults | Select-Object -Last $Config.MaxResultsShown))
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