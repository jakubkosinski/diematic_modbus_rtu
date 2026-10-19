# Changelog

## 0.2.3

- The *Boiler error* sensor now shows an unknown state when the boiler reports a code that is not
  in the error list, instead of keeping the previous error on screen.

## 0.2.2

- The day and night DHW temperature settings now go down to 10 °C, as on the boiler itself; they
  were limited to 40 °C.

## 0.2.1

- Fixed the *Boiler time* sensor showing a time shifted by your offset from UTC (two hours in
  Poland) when no time zone had been set for the app. The zone is now taken from the
  configuration, then from `TZ` (which Home Assistant passes to the app, so the app now follows
  Home Assistant's time zone), then from the machine, and the zone in use is logged at start. If
  none is found the app falls back to UTC and says so in a warning.

## 0.2.0

- New *Boiler time* sensor: the boiler's date and time registers combined into one timestamp,
  shown in the time zone of the app (Home Assistant's), so a drifting boiler clock is easy to spot.
- The clock registers are now read at the measurement interval instead of the slow settings
  interval, so the time stays current.

## 0.1.0

- First release: Modbus RTU over a TCP converter, MQTT with Home Assistant discovery.
