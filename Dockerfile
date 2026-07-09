FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY generate.py api-docs.yml ./
# regenerate server.py from the pinned spec + allowlist at build time,
# so the image can never drift from the curated surface in generate.py
RUN python generate.py

ENTRYPOINT ["python3", "server.py"]
