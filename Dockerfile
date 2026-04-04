FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl \
      tzdata \
      nodejs \
      npm \
      fonts-dejavu-core \
      fontconfig \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8787"]