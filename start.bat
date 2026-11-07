@echo off
REM Starts the paper-trading bot and the dashboard in two windows.
cd /d "%~dp0"
start "Paper bot (close to stop)" cmd /k ".venv\Scripts\python.exe run.py"
start "Dashboard" cmd /k ".venv\Scripts\streamlit.exe run dashboard.py"
