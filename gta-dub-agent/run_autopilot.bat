@echo off
cd /d "%~dp0"
if not exist output mkdir output
python -m dub_agent autopilot >> output\autopilot_console.log 2>&1
