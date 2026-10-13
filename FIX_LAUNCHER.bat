@echo off
title Sora in Borderlands - fix "Initialization error" when starting Borderlands
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\setup.ps1" -FixLauncher %*
echo.
pause
