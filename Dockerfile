FROM python:3.13-slim

WORKDIR /app

# Install FFmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Ensure downloads directory exists
RUN mkdir -p downloads

EXPOSE 8000

# The container is the shared public instance, so hosted guardrails apply.
# Desktop/native builds ship their own defaults and never set this.
ENV MEDIAGRAB_MODE=hosted
ENV BEHIND_PROXY=false

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
