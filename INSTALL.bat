@echo off
title Sora in Borderlands - installer
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\setup.ps1" %*
echo.
pause
