<#
    Search-MoviesOnComputer.ps1

    Regenerates a movie list from your Letterboxd export (via
    movie_list_export.py), then searches for each title on disk using
    the "Everything" search tool (Search-Everything module), logging
    results to a Markdown file.

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

function Get-MatchConfidence {
    <#
        Returns 'Strong' when a result's filename contains both the
        movie's release year AND a resolution tag (480p/720p/1080p/2160p/
        4K), 'Basic' when it contains just the year, or 'None' when
        neither is present. Title text alone is what short titles like
        "M" or "Pi" abuse to produce false positives, so the year is the
        real gate - resolution on top is extra reassurance, not a
        requirement, which is why it earns its own, slightly softer tier
        rather than being required outright.
    #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string]$Year
    )

    if (-not $Year -or $FilePath -notmatch [regex]::Escape($Year)) {
        return 'None'
    }

    if ($FilePath -match '(?i)\b(480p|720p|1080p|2160p|4k)\b') {
        return 'Strong'
    }

    return 'Basic'
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
if (Test-Path $OutputFile) {
    Remove-Item -Path $OutputFile
}

Set-Location $Config.WorkingDir

# Regenerate the movie list JSON from the latest Letterboxd export.
& $Config.PythonExe $Config.PythonScript

if (-not (Test-Path $Config.MovieListJson)) {
    throw "Movie list JSON not found at $($Config.MovieListJson). Did the Python script run correctly?"
}

# Each entry is {Title, Year} rather than a bare string, so the
# confidence check below has a year to work with.
$movieEntries = Get-Content -Path $Config.MovieListJson -Raw | ConvertFrom-Json

$MoviesWithNoResults = 0
$MoviesWithResults   = 0
$MoviesOnlyOnFriendsServer          = 0
$MoviesWithNoLocalAndRemoteResults  = 0

# Buffer every line in memory and write the file once at the end, instead
# of opening/closing the output file on every single Out-File -Append call.
$resultsBuffer = [System.Collections.Generic.List[string]]::new()

$totalMovies = @($movieEntries).Count
$i = 0

# Get-SearchFriendlyTitle and Format-ServerNameList live in
# MovieSearchHelpers.ps1, dot-sourced above.

$remoteCheckRan = [bool]($Config.PlexAccountToken -and $Config.FriendServerNames -and $Config.FriendServerNames.Count -gt 0)

if ($remoteCheckRan) {
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

    if ($i % 5 -eq 0 -or $i -eq $totalMovies) {
        Write-Progress -Activity "Creating Results List" `
            -Status "Processed $i of $totalMovies movies" `
            -PercentComplete (($i / $totalMovies) * 100)
    }

    $movie = $movieEntry.Title
    $year  = $movieEntry.Year

    $searchResults = Search-Everything -Global -Filter $movie -Extension $Config.Extensions -MatchWholeWord
    $matchedTitle  = $movie

    if ($null -eq $searchResults) {
        $cleanTitle = Get-SearchFriendlyTitle -Title $movie
        if ($cleanTitle -and $cleanTitle -ne $movie) {
            $searchResults = Search-Everything -Global -Filter $cleanTitle -Extension $Config.Extensions -MatchWholeWord
            if ($searchResults) {
                $matchedTitle = $cleanTitle
            }
        }
    }

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

    if ($foundLocally -and $ShowMode -in 'ResultsOnly', 'Both') {
        $resultsToShow = @($searchResults | Select-Object -Last $Config.MaxResultsShown)

        $confidenceByResult = @{}
        foreach ($result in $resultsToShow) {
            $confidenceByResult[$result] = Get-MatchConfidence -FilePath $result -Year $year
        }
        $bestConfidence = 'None'
        if ('Strong' -in $confidenceByResult.Values) { $bestConfidence = 'Strong' }
        elseif ('Basic' -in $confidenceByResult.Values) { $bestConfidence = 'Basic' }

        $resultCount = @($searchResults).Count
        $resultWord  = if ($resultCount -eq 1) { 'result' } else { 'results' }
        $emoji = switch ($bestConfidence) {
            'Strong' { '✅' }
            'Basic'  { '🟡' }
            default  { '⚠️' }
        }

        $resultsBuffer.Add("### $emoji '$movie' — $resultCount $resultWord")

        switch ($bestConfidence) {
            'Strong' { $resultsBuffer.Add('_Confident match — title, year & resolution all present_') }
            'Basic'  { $resultsBuffer.Add('_Confident match — title and year are present_') }
            default  { $resultsBuffer.Add('_Low confidence — verify manually_') }
        }
        if ($matchedTitle -ne $movie) {
            $resultsBuffer.Add(('_Matched via cleaned title: `{0}`_' -f $matchedTitle))
        }
        if ($onFriendsServer) {
            $resultsBuffer.Add("_Also available on $(Format-ServerNameList -Names $serverNames) Plex server_")
        }
        $resultsBuffer.Add('')

        foreach ($result in $resultsToShow) {
            if ($confidenceByResult[$result] -ne 'None') {
                $resultsBuffer.Add(('- **`{0}`**' -f $result))
            }
            else {
                $resultsBuffer.Add(('- `{0}`' -f $result))
            }
        }
        $resultsBuffer.Add('')
        $resultsBuffer.Add('---')
        $resultsBuffer.Add('')
    }

    if (-not $foundLocally -and $ShowMode -in 'NoResultsOnly', 'Both') {
        $resultsBuffer.Add("### ❌ '$movie'")
        $resultsBuffer.Add('_Did not return any results._')
        if ($onFriendsServer) {
            $resultsBuffer.Add("_Available on $(Format-ServerNameList -Names $serverNames) Plex server_")
        }
        $resultsBuffer.Add('')
        $resultsBuffer.Add('---')
        $resultsBuffer.Add('')
    }

    if (-not $foundLocally -and -not $onFriendsServer -and $ShowMode -eq 'NoLocalAndRemoteResultsOnly') {
        $resultsBuffer.Add("### ❌ '$movie'")
        $resultsBuffer.Add("_Not found on your computer, and not available on $(Format-ServerNameList -Names $Config.FriendServerNames -Conjunction 'or') Plex server._")
        $resultsBuffer.Add('')
        $resultsBuffer.Add('---')
        $resultsBuffer.Add('')
    }
}

Write-Progress -Activity "Creating Results List" -Completed

# ---------------------------------------------------------------------------
# Header - written last, since it needs final totals, then placed first
# ---------------------------------------------------------------------------
$modeLabel = switch ($ShowMode) {
    'ResultsOnly'                 { 'Movies With Results' }
    'NoResultsOnly'               { 'Movies With No Results' }
    'Both'                        { 'All Movies' }
    'NoLocalAndRemoteResultsOnly' { 'Movies Not Found Anywhere' }
}

$header = [System.Collections.Generic.List[string]]::new()
$header.Add("# Movie Search Results — $modeLabel")
$header.Add('')
$header.Add("_$MoviesWithResults of $totalMovies movies found locally._")
if ($remoteCheckRan) {
    $serverList = Format-ServerNameList -Names $Config.FriendServerNames
    $header.Add("_Of the $MoviesWithNoResults not found locally, $MoviesOnlyOnFriendsServer are available on $serverList Plex server._")
}
$header.Add('')
$header.Add('---')
$header.Add('')

$finalLines = [System.Collections.Generic.List[string]]::new()
$finalLines.AddRange($header)
$finalLines.AddRange($resultsBuffer)
$finalLines | Out-File -FilePath $OutputFile -Encoding utf8

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