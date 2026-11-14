# Team Calendar

A lightweight internal web application for sharing team schedules, to-dos, work items, and related documents.

## Features

### Calendar and events
- Monthly and weekly calendar views
- Create and edit shared or personal events
- Event categories such as SR, CM, meetings, leave, and other
- Optional environment, assignee, and ticket information for work-related events
- Comments and file attachments

### To-dos and work tracking
- Personal and shared to-dos
- Due dates, priorities, assignees, notes, and status
- Kanban-style task overview and list pagination
- Search and filters for day-to-day work

### Administration
- User and schedule management
- Activity history and deleted-item review
- Recovery of supported deleted records

## Technology

| Area | Stack |
| --- | --- |
| Backend | Python, FastAPI |
| Database | SQLite |
| Templates | Jinja2 |
| Runtime | uv, Uvicorn |
| Frontend | HTML, CSS, JavaScript |

## Run locally

Requirements: Python 3.11 or later and uv.

    git clone https://github.com/BaeDoge/python-share-calender.git
    cd python-share-calender
    uv sync
    uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

Open http://127.0.0.1:8000.

To make the application available to other machines on a trusted network, configure the host and network access according to your environment. Do not expose it publicly without additional security controls.

## Data storage

The application stores its SQLite database, attachments, and related runtime data under the project data directory. Back up this directory regularly and protect it from unauthorised access.

## Security considerations

The current user identification flow uses a browser-generated identifier and connection IP information rather than a full account-based authentication system. It is intended for a trusted internal environment. A public or organisation-wide deployment should add appropriate authentication, authorisation, HTTPS, backup, and data-retention controls.
