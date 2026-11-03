# devices/api/ed_device.py uses PEP 695 `type` aliases — Python 3.12 is the minimum.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/webapp
# Keeps logs/ and backups/ inside the app directory instead of the filesystem root.
ENV MECADEMIC_DEMO_ROOT=/webapp

WORKDIR /webapp

# libusb: pyusb's backend, which pyvisa-py needs to reach USB instruments
# (Thorlabs power meters). The Spinnaker GenTL producer links against it too.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Spinnaker GenTL producer for Teledyne FLIR GigE Vision / USB3 Vision cameras
# (gige_camera devices), taken from the SDK bundle in ressources/. Only the
# libgentl package is installed: the .cti is self-contained and needs nothing
# but libusb. Its preinst refuses to run until the FLIR Spinnaker SDK EULA is
# accepted through debconf, so the answer is preseeded here. The bundle is
# bind-mounted, so it never lands in an image layer.
# In config.yaml: cti_path: "/opt/spinnaker/lib/spinnaker-gentl/Spinnaker_GenTL.cti"
RUN --mount=type=bind,source=ressources,target=/tmp/ressources \
    mkdir /tmp/spinnaker \
    && tar -xzf /tmp/ressources/spinnaker-*-amd64-pkg.tar.gz -C /tmp/spinnaker \
        --strip-components=1 --wildcards '*/libgentl_*.deb' \
    && echo 'libgentl libspinnaker/accepted-flir-eula boolean true' | debconf-set-selections \
    && DEBIAN_FRONTEND=noninteractive dpkg -i /tmp/spinnaker/libgentl_*.deb \
    && rm -rf /tmp/spinnaker
# What the SDK's configure_gentl_paths.sh exports for login shells.
ENV SPINNAKER_GENTL64_CTI=/opt/spinnaker/lib/spinnaker-gentl/Spinnaker_GenTL.cti
ENV GENICAM_GENTL64_PATH=/opt/spinnaker/lib/spinnaker-gentl

COPY requirements.txt .
COPY ressources/*.whl ./ressources/
RUN pip install --no-cache-dir -r requirements.txt

COPY --exclude=ressources/spinnaker-*.tar.gz . .

EXPOSE 5000

# --workspace must point at a directory containing app_logic/ (config.yaml,
# the optional context.yaml and the task modules); that is /webapp in this image.
CMD ["python", "app.py", "--workspace", "/webapp", "--host", "127.0.0.1", "--port", "5000"]
