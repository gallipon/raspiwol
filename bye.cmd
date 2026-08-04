@echo off
rem Leave-work command: wrapper for pcsleep_cmd.py.
rem Put this in a directory on PATH (e.g. %USERPROFILE%\.local\bin) to run `bye`.
rem   bye          sleep in 10 min (default)
rem   bye 20       sleep in 20 min
rem   bye now      sleep immediately
rem   bye cancel   cancel a pending reservation
rem ASCII only: cmd.exe reads .cmd as the OEM codepage, and Japanese comments
rem containing 0x5C bytes break the parser.
python "%USERPROFILE%\pcsleep_cmd.py" %*
