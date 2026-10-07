# devices/api/ed_device.py uses PEP 695 `type` aliases — Python 3.12 is the minimum.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/webapp
# Keeps logs/ and backups/ inside the app directory instead of the filesystem root.
ENV MECADEMIC_DEMO_ROOT=/webapp

WORKDIR /webapp

# libusb: pyusb's backend, which pyvisa-py needs to reach USB instruments
# (Thorlabs power meters).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
COPY ressources/ ./ressources/
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# --workspace must point at a directory containing app_logic/ (config.yaml,
# the optional context.yaml and the task modules); that is /webapp in this image.
CMD ["python", "app.py", "--workspace", "/webapp", "--host", "0.0.0.0", "--port", "5000"]
