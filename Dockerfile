FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ ./app/

# The shared JupyterHub data directory is bind-mounted at /jupyterhub_data at
# runtime. Runs as root so chmod on dirs created by JupyterHub's (also-root)
# pre_spawn_hook succeeds — see _set_mode in app/routers/provision.py.
RUN mkdir -p /jupyterhub_data

EXPOSE 6060

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "6060"]
