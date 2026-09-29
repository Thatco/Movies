<#
    MovieSearchHelpers.ps1

    Shared helper functions used by both Search-MoviesOnComputer.ps1 and
    API_call.ps1. Dot-source this file before calling Get-SearchFriendlyTitle
    from either script - that way the title-cleaning logic only has to live
    in one place instead of being copied into two and risking drifting out
    of sync later.
#>

function Get-SearchFriendlyTitle {
    <#
        Filenames on disk - and, it turns out, Plex's own movie titles -
        rarely keep a title's exact punctuation: colons, ellipses, dashes,
        commas and so on tend to get dropped or swapped out. This collapses
        any run of non-letter, non-digit characters down to a single space,
        so "Spider-Man: Into the Spider-Verse" becomes
        "Spider Man Into the Spider Verse" for comparison purposes.

        \p{L} and \p{Nd} match Unicode letters/digits rather than just
        a-z/0-9, so accented titles aren't mangled in the process.
    #>
    param([Parameter(Mandatory)][string]$Title)

    ($Title -replace '[^\p{L}\p{Nd}]+', ' ').Trim()
}

function Format-ServerNameList {
    <#
        Joins server names into a natural, Oxford-comma phrase, each with
        a possessive 's already attached:
            @('Friend1')                    -> "Friend1's"
            @('Friend1','Friend2')          -> "Friend1's and Friend2's"
            @('Friend1','Friend2','Friend3') -> "Friend1's, Friend2's, and Friend3's"

        -Conjunction 'or' swaps the final joiner, for a negative-mode
        phrasing like "not on Friend1's, Friend2's, or Friend3's server."
    #>
    param(
        [Parameter(Mandatory)][string[]]$Names,
        [string]$Conjunction = 'and'
    )

    $possessives = $Names | ForEach-Object { "$_'s" }

    switch ($possessives.Count) {
        0 { return '' }
        1 { return $possessives }
        2 { return "$($possessives[0]) $Conjunction $($possessives[1])" }
        default {
            $allButLast = $possessives[0..($possessives.Count - 2)] -join ', '
            return "$allButLast, $Conjunction $($possessives[-1])"
        }
    }
}