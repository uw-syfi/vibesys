# The task image: Python with aiohttp (hash-pinned in requirements.txt) and this directory at /task.
# It runs accuracy_checker/checker.py and benchmark/run.py; it has no GPU and reaches the
# candidate's server only over HTTP (VIBESYS_SERVICE_URL). vibesys-cli builds it with this
# directory as the context, never from a candidate's checkout. The base is pinned by digest.
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
COPY requirements.txt /tmp/task-requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/task-requirements.txt \
 && rm /tmp/task-requirements.txt

COPY . /task
# A job runs as the invoking user, whose HOME may not exist.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
RUN python -I /task/accuracy_checker/checker.py --help >/dev/null \
 && python -I /task/benchmark/run.py --help >/dev/null \
 && python -I -c "import sys; sys.path.insert(0, '/task/accuracy_checker'); import checker; checker.load_reference()"
