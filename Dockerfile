FROM python:3.12-slim

# Non-root user for security
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ ./app/

# The shared JupyterHub data directory is bind-mounted at /jupyterhub_data at
# runtime.  Create the mount-point so Docker doesn't auto-create it as root.
RUN mkdir -p /jupyterhub_data && chown appuser:appuser /jupyterhub_data

USER appuser

EXPOSE 6060

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "6060"]
