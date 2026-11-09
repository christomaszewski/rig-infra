# zenoh-store: python + the fleet's zenoh-python pin + the zenoh_store package. No ROS.
# PYTHON_IMAGE lets an air-gapped builder point at a mirrored copy of the same base.
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
ARG ZENOH_VERSION=1.9.0
RUN pip install --no-cache-dir "eclipse-zenoh==${ZENOH_VERSION}"
COPY zenoh_store /opt/zenoh-store/zenoh_store
ENV PYTHONPATH=/opt/zenoh-store PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "-m", "zenoh_store"]
