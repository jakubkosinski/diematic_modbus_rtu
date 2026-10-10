# Built by CI with the repository root as the build context (see .github/workflows/app-image.yml).
FROM ghcr.io/home-assistant/base-python:3.12-alpine3.23

ARG BUILD_VERSION
LABEL io.hass.type="app" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.name="Diematic Modbus" \
      io.hass.description="De Dietrich Diematic iSystem over Modbus RTU and MQTT"

COPY pyproject.toml README.md LICENSE /tmp/diematic/
COPY src /tmp/diematic/src
RUN pip install --no-cache-dir /tmp/diematic && rm -rf /tmp/diematic

COPY diematic_modbus/run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
