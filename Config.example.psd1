fig.example · PSD1
@{
    # Copy this file to Config.local.psd1 (gitignored) and fill in your
    # own paths. Search-MoviesOnComputer.ps1 reads Config.local.psd1, not
    # this one - this file exists purely as a checked-in template so
    # anyone cloning the repo (including future you, on another machine)
    # knows what to fill in.
 
    PythonExe              = 'C:\Path\To\python.exe'
    PythonScript           = 'C:\Path\To\movie_list_export.py'
    WorkingDir             = 'C:\Path\To\WorkingDirectory'
    MovieListJson          = 'C:\Path\To\MovieList.json'
    MovieResultsFile              = 'C:\Path\To\MovieResultsFile.md'
    MovieWithNoResultsFile        = 'C:\Path\To\MovieWithNoResultsFile.md'
    AllMoviesFile                 = 'C:\Path\To\AllMoviesFile.md'
    NoResultsNotOnFriendsServerFile = 'C:\Path\To\NoResultsNotOnFriendsServerFile.md'
    Extensions             = @('mkv', 'mp4', 'avi', 'wmv')
    MaxResultsShown        = 50
 
    # Optional: leave PlexAccountToken/FriendServerNames blank to skip the
    # remote-server check entirely.
    PlexAccountToken       = ''
    PlexClientIdentifier   = 'PowerShell-MovieSearch'
    FriendServerNames      = @()   # e.g. @('Friend1', 'Friend2', 'Friend3')
    RemoteMoviesFolder     = 'C:\Path\To\Output Text Files'
    LibraryDefaultsFile    = 'C:\Path\To\Config Text Files\LibraryDefaults.psd1'
 
    # Manually confirmed title -> file path(s), set via Confirm-MovieMatch.ps1.
    ConfirmedMatchesFile   = 'C:\Path\To\Config Text Files\ConfirmedMatches.psd1'
}