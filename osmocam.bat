@echo off
rem OsmoCam: the camera as a webcam (arguments pass through, e.g. osmocam.bat --no-vcam).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0osmocam.ps1" %*
rem Keep the window open after an error, so the message can be read.
if errorlevel 1 pause
