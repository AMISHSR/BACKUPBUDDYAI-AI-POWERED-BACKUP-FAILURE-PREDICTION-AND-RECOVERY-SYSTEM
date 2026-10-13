#!/bin/sh
[ -d venv ] || python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
python app.py
