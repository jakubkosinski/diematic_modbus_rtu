# Changelog

## 0.2.0

- New *Boiler time* sensor: the boiler's date and time registers combined into one timestamp,
  shown in the time zone of the app (Home Assistant's), so a drifting boiler clock is easy to spot.
- The clock registers are now read at the measurement interval instead of the slow settings
  interval, so the time stays current.

## 0.1.0

- First release: Modbus RTU over a TCP converter, MQTT with Home Assistant discovery.
