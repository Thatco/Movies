<#
    Search-MoviesOnComputer.ps1

    Regenerates a movie list from your Letterboxd export (via
    movie_list_export.py), then searches for each title on disk using
    the "Everything" search tool (Search-Everything module), logging
    results to a text file.
#>

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
# ---------------------------------------------------------------------------
do {
    $answer = Read-Host "Would you like to show movies with results (1), movies with no results (2), or both (3)?"
} while ($answer -notin '1', '2', '3')

$ShowMode = switch ($answer) {
    '1' { 'ResultsOnly' }
    '2' { 'NoResultsOnly' }
    '3' { 'Both' }
}

$OutputFile = switch ($ShowMode) {
    'ResultsOnly'   { $Config.MovieResultsFile }
    'NoResultsOnly' { $Config.MovieWithNoResultsFile }
    'Both'          { $Config.AllMoviesFile }
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

# Buffer every line in memory and write the file once at the end, instead
# of opening/closing the output file on every single Out-File -Append call.
$resultsBuffer = [System.Collections.Generic.List[string]]::new()

# $movieTitles already came from the JSON we just parsed, so the total is
# just its length - no need to hardcode a count or call back into Python.
$totalMovies = @($movieTitles).Count
$i = 0

# Get-SearchFriendlyTitle now lives in MovieSearchHelpers.ps1, dot-sourced
# above, since API_call.ps1 needs the same normalization on Plex titles.

# Only attempt the friend's-server check if it's actually configured -
# keeps this optional rather than a hard requirement to run the script.
if ($Config.PlexAccountToken -and $Config.FriendServerName) {
    $remoteTitles = Get-RemoteMovieTitles -AccountToken $Config.PlexAccountToken `
        -ClientIdentifier $Config.PlexClientIdentifier `
        -FriendServerName $Config.FriendServerName `
        -RemoteMoviesFile $Config.RemoteMoviesFile
}
else {
    $remoteTitles = [System.Collections.Generic.HashSet[string]]::new()
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

    if ($null -eq $searchResults) {
        $MoviesWithNoResults++

        # Only worth checking the remote set once both local attempts have
        # already failed - this still counts as "no results" locally (it's
        # not on your disk), the remote server is just extra context.
        $onFriendsServer = $remoteTitles.Contains((Get-SearchFriendlyTitle -Title $movie))

        # Counters above always update so the summary stays accurate -
        # only whether this gets WRITTEN to the file depends on $ShowMode.
        if ($ShowMode -in 'NoResultsOnly', 'Both') {
            $line = "'$movie' did not return any results."
            if ($onFriendsServer) {
                $line += " (available on $($Config.FriendServerName)'s Plex server)"
            }
            $resultsBuffer.Add($line)
            $resultsBuffer.Add('')
        }
    }
    else {
        $resultCount = @($searchResults).Count
        $MoviesWithResults++

        if ($ShowMode -in 'ResultsOnly', 'Both') {
            $resultWord = if ($resultCount -eq 1) { 'result' } else { 'results' }
            $line = "'$movie' returned $resultCount $resultWord."
            if ($matchedTitle -ne $movie) {
                $line += " (matched via cleaned title: '$matchedTitle')"
            }

            $resultsBuffer.Add($line)
            $resultsBuffer.Add(('-' * 68))
            $resultsBuffer.AddRange([string[]]($searchResults | Select-Object -Last $Config.MaxResultsShown))
            $resultsBuffer.Add('')
        }
    }
}

Write-Progress -Activity "Creating Results List" -Completed

$resultsBuffer | Out-File -FilePath $OutputFile -Encoding utf8 -Width 200

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host "$(Split-Path $OutputFile -Leaf) updated at $OutputFile"
Write-Host "There are $MoviesWithNoResults movies with no results."
Write-Host "There are $MoviesWithResults movies with results."
