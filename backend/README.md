# HRL-CRMS Backend

A Flask-based REST API for a distributed computing platform, designed as a project for educational environments. Enables students to request and execute resource-intensive tasks (e.g., dev software, simulations, ML training) on remote agent nodes.

## Features
- JWT authentication with role-based access.
- Intelligent task allocation with resource monitoring.
- Distributed ML training and result aggregation.
- Supabase integration for database and storage.

## Tech Stack
- **Framework**: Flask
- **Database**: PostgreSQL (Supabase) via SQLAlchemy
- **Auth**: JWT
- **ML**: Pandas
- **Deployment**: Gunicorn, Render

## Installation
1. Clone: `git clone https://github.com/HRL-CRMS/Backend.git`
2. Install: `pip install -r requirements.txt`
3. Set env vars in `.env` (DATABASE_URL, JWT_SECRET_KEY, etc.)
4. Run: `python run.py`

## Usage
- API endpoints: `/auth/login`, `/tasks/request`, etc.
- Deploy with Render using `Procfile`.

## Deployment
- Push to Git, set env vars in dashboard. Uses Procfile.

## Limitations
-  Supabase-dependent.
