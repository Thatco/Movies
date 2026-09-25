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
