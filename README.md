# ShortFlow Uploader

ShortFlow Uploader organizes videos, metadata and release schedules before guiding the YouTube Studio upload process. Its desktop interface keeps preparation together and pauses for review before final publication.

## Features

- Pair local videos with titles, descriptions and other metadata.
- Prepare schedules through a dedicated desktop interface.
- Drive the Studio workflow with Playwright.
- Stop before final publication by default.

## How it works

The GUI prepares configuration and hands a batch to browser automation. Local logs and a separate browser profile belong to the person running the application.

**Stack:** Python · Tkinter · Playwright

## Getting started

Use Python 3.12 and a separate virtual environment. Run the following commands from this repository's root in Windows PowerShell.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-local.txt
```

The local requirements include Playwright. Install Google Chrome separately; the uploader uses the installed Chrome browser with a dedicated local profile.

### Start the application

Start the GUI, then use its “Open YouTube channel” control (labelled “Открыть YouTube канал”) to open the dedicated Chrome profile. Sign in to your own test channel and keep that Chrome window open while uploading. The default settings preserve `stop_before_publish=true`.

```powershell
Copy-Item config.example.json config.json
python gui.py
```

## Example workflow

Pair a sample video with its title and description, inspect the proposed schedule, and review the publishing checkpoint.

## Testing and limitations

Local video/metadata matching, schedule preparation and the stop-before-publish defaults were checked. No browser login or upload was performed. A connected preview can still create a draft upload.

See [Verification](VERIFICATION.md) for the recorded checks and [Limitations](LIMITATIONS.md) for integration requirements.

## Configuration and security

The default configuration uses private visibility and stops before final publication. Browser profiles and channel settings stay on your machine. Configure your own provider credentials when a feature requires them; credentials and personal data are not included. See [Security](SECURITY.md) for local configuration and reporting guidance.
