# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.9.6

FROM python:${PYTHON_VERSION}-slim

LABEL fly_launch_runtime="flask"

WORKDIR /code

COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt

COPY . .

EXPOSE 8080

# One worker with several threads: SQLite and the in-memory caches live in a
# single process, and the small VM has little RAM. gunicorn is a real
# production server (the old "flask run" is a development server).
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "120", "--worker-tmp-dir", "/dev/shm", "--access-logfile", "-"]
