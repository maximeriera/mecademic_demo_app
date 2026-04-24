@echo off

REM Replace C:\Path\To\Your\Flask\App with the actual path to your project folder
cd /d "C:\Users\BENDERMECA\Documents\mecademic_demo_app"

REM Replace .venv with the name of your virtual environment folder
REM This calls the activate script inside your venv
call .venv\Scripts\activate.bat

REM Replace your_flask_app.py with the name of your main Flask application file
REM The 'start' command runs the Flask app in a new, non-blocking window
start python app.py

REM Optional: If your script has an environment variable for the run command (like 'flask run'):
REM start flask run

REM Optional: Deactivate the venv, though it's not strictly necessary since the process will exit
REM call deactivate