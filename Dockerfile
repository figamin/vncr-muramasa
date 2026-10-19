FROM python:3.11-slim

# Install system dependencies (CJK fonts for chart rendering)
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-cjk \
    fonts-noto-cjk-extra \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /root/.cache/matplotlib /root/.matplotlib

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Web console port
EXPOSE 5000

CMD ["python", "main.py"]
