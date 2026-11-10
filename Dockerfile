ARG PYTHON_IMAGE=platform/python314:ubuntu26.04-20261109-r2
FROM ${PYTHON_IMAGE}
# This application uses only the standard library; packaging tools stay in build images.
RUN python3 -m pip --disable-pip-version-check uninstall -y pip \
    && apt-get purge -y python3-pip-whl python3-setuptools-whl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY backend ./backend
COPY web ./web
USER 65532:65532
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 CMD ["python3", "-B", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3).read()"]
CMD ["python3", "-B", "-m", "backend.app"]
