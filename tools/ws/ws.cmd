@echo off
set PY=python
where py >nul 2>nul && set PY=py -3
%PY% "%~dp0ws.py" %*
