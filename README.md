# CivicClean

Report. Clean. Transform. Citizens report garbage with a photo, workers clean it and upload an after photo, admins approve.

Backend: **Python (Flask)**. Database: **SQLite** (`civicclean.db`, created automatically).

## Roles (logins are prefilled)
- **Citizen**: live square camera or upload; GPS and time are stamped on the photo.
- **Worker** (passcode `worker123`): sees pending reports, uploads the after photo.
- **Admin** (passcode `admin123`): compares before/after and approves or sends back.

## Files
```
app.py              Flask server, API and SQLite database
requirements.txt    Python packages
Procfile            start command for hosting (gunicorn)
static/index.html   front page
static/css/style.css
static/js/          core, card, citizen, worker, admin, app
```

## Run on your computer
```
pip install -r requirements.txt
python app.py
```
Open http://localhost:5000

## Deploy (GitHub holds the code, a Python host runs it)
GitHub Pages cannot run Python, so:
1. Upload this whole folder to a GitHub repository.
2. On https://render.com create a **Web Service** from that repository.
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`
3. Add environment variables: `SECRET_KEY` (any long random text), `WORKER_PASSCODE`, `ADMIN_PASSCODE`.

Camera and GPS need HTTPS, which Render provides.

## Notes
- On free hosting the disk can reset, which erases the SQLite file. For permanent data use a persistent disk (set `DATABASE_PATH`) or move to PostgreSQL.
- Change the default passcodes and `SECRET_KEY` before real use.
