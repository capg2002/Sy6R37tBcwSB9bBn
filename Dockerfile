FROM python:3.14-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements-*.txt ./
RUN python -m pip install -r requirements-docker.txt && python -m pip check \
    && python -m pip freeze > /opt/build-requirements.txt
RUN groupadd --gid 10001 trader && useradd --uid 10001 --gid trader --no-create-home trader
COPY --chown=10001:10001 . .
USER 10001:10001
CMD ["python", "deploy/preflight.py"]
