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

function Get-SearchFriendlyTitle {
    <#
        Filenames on disk rarely keep a title's exact punctuation - colons,
        ellipses, dashes, commas, and so on tend to get dropped or swapped
        out entirely when a file is ripped or downloaded. This collapses
        any run of non-letter, non-digit characters down to a single space,
        so "Spider-Man: Into the Spider-Verse" becomes
        "Spider Man Into the Spider Verse" for a fallback search.

        \p{L} and \p{Nd} match Unicode letters/digits rather than just
        a-z/0-9, so accented titles aren't mangled in the process.
    #>
    param([Parameter(Mandatory)][string]$Title)

    ($Title -replace '[^\p{L}\p{Nd}]+', ' ').Trim()
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

        # Counters above always update so the summary stays accurate -
        # only whether this gets WRITTEN to the file depends on $ShowMode.
        if ($ShowMode -in 'NoResultsOnly', 'Both') {
            $resultsBuffer.Add("'$movie' did not return any results.")
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

# Amph. Amph. 

Write-Progress -Activity "Creating Results List" -Completed

$resultsBuffer | Out-File -FilePath $OutputFile -Encoding utf8 -Width 200

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host "$(Split-Path $OutputFile -Leaf) updated at $OutputFile"
Write-Host "There are $MoviesWithNoResults movies with no results."
Write-Host "There are $MoviesWithResults movies with results."