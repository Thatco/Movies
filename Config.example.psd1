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
    MovieResultsFile       = 'C:\Path\To\MovieResultsFile.txt'
    MovieWithNoResultsFile = 'C:\Path\To\MovieWithNoResultsFile.txt'
    AllMoviesFile          = 'C:\Path\To\AllMoviesFile.txt'
    Extensions             = @('mkv', 'mp4', 'avi', 'wmv')
    MaxResultsShown        = 50

    # Optional: leave PlexAccountToken/FriendServerName blank to skip the
    # remote-server check entirely.
    PlexAccountToken       = ''
    PlexClientIdentifier   = 'PowerShell-MovieSearch'
    FriendServerName       = ''
    RemoteMoviesFile       = 'C:\Path\To\RemoteMovies.txt'
}
