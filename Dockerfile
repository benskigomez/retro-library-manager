FROM python:3.12-slim-trixie

# unzip/zipinfo, 7-Zip (7zz) and chdman (mame-tools; no full MAME binary needed)
RUN apt-get update \
 && apt-get install -y --no-install-recommends unzip 7zip mame-tools tini \
 && rm -rf /var/lib/apt/lists/* \
 && command -v chdman 7zz unzip zipinfo

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV RLM_ROMS=/roms \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8787
# Unraid convention: nobody:users. Files are created a+rwX (umask 000 + explicit chmod).
USER 99:100
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/healthz')"
ENTRYPOINT ["tini", "--"]
CMD ["sh", "-c", "umask 000 && exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8787 --proxy-headers"]
