#!/usr/bin/with-contenv bashio
# Translates the app options (and the Supervisor's MQTT service) into the environment that
# `diematic run` reads. Secrets are exported but never logged.

export DIEMATIC_HOST="$(bashio::config 'gateway_host')"
export DIEMATIC_PORT="$(bashio::config 'gateway_port')"
export DIEMATIC_MAP="$(bashio::config 'map')"
export DIEMATIC_LANGUAGE="$(bashio::config 'language')"
export DIEMATIC_READ_ONLY="$(bashio::config 'read_only')"
export DIEMATIC_TRAILING_ZERO="$(bashio::config 'trailing_zero')"
export DIEMATIC_FAST_INTERVAL="$(bashio::config 'fast_interval')"
export DIEMATIC_SLOW_INTERVAL="$(bashio::config 'slow_interval')"
export DIEMATIC_CLOCK_SYNC="$(bashio::config 'clock_sync')"
export DIEMATIC_LOG_LEVEL="$(bashio::config 'log_level')"
export DIEMATIC_TIMEZONE="$(bashio::supervisor.timezone)"
export MQTT_TOPIC_PREFIX="$(bashio::config 'mqtt_topic_prefix')"
export MQTT_DISCOVERY_PREFIX="$(bashio::config 'mqtt_discovery_prefix')"

if bashio::config.has_value 'slave_address'; then
    export DIEMATIC_SLAVE_ADDRESS="$(bashio::config 'slave_address')"
fi

# A custom register map placed in the app's configuration folder is addressed as /config/<file>.
if [[ "${DIEMATIC_MAP}" != /* && "${DIEMATIC_MAP}" == *.yaml && -f "/config/${DIEMATIC_MAP}" ]]; then
    export DIEMATIC_MAP="/config/${DIEMATIC_MAP}"
fi

if [[ -z "${DIEMATIC_HOST}" ]]; then
    bashio::exit.nok "Set gateway_host to the address of the RS485-to-Ethernet converter."
fi

if bashio::config.has_value 'mqtt_host'; then
    export MQTT_HOST="$(bashio::config 'mqtt_host')"
    # Unset optional options read back as the text "null", so only export the ones that are set.
    if bashio::config.has_value 'mqtt_port'; then
        export MQTT_PORT="$(bashio::config 'mqtt_port')"
    fi
    if bashio::config.has_value 'mqtt_username'; then
        export MQTT_USERNAME="$(bashio::config 'mqtt_username')"
    fi
    if bashio::config.has_value 'mqtt_password'; then
        export MQTT_PASSWORD="$(bashio::config 'mqtt_password')"
    fi
elif bashio::services.available 'mqtt'; then
    bashio::log.info "Using the MQTT broker provided by Home Assistant."
    export MQTT_HOST="$(bashio::services 'mqtt' 'host')"
    export MQTT_PORT="$(bashio::services 'mqtt' 'port')"
    export MQTT_USERNAME="$(bashio::services 'mqtt' 'username')"
    export MQTT_PASSWORD="$(bashio::services 'mqtt' 'password')"
else
    bashio::exit.nok "No MQTT broker: install the Mosquitto app or set mqtt_host."
fi

bashio::log.info "Starting the bridge for ${DIEMATIC_HOST}:${DIEMATIC_PORT}."
exec diematic run
