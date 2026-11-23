# Ring light controller (ESP32, firmware v1.3) - protocol

TCP, default port 8888. Line based: every command and every reply ends with `\n`.
Every command gets exactly one reply line.

| command | reply |
|---|---|
| `ID?` | `RINGLIGHT,v1.3,<mac>` |
| `CH<n>:LEVEL <0-255>` | `OK` |
| `CH<n>:LEVEL?` | `<0-255>` |
| `CH<n>:ON` / `CH<n>:OFF` | `OK` |
| `CH<n>:STATE?` | `ON` or `OFF` |
| `STROBE <ms>` | `OK` once the flash is done (1-500 ms, all channels that are ON) |
| `TEMP?` | board temperature in C, like `41.5` |

n is 0 to 3 (4 channels).

Errors: `ERR <text>`, e.g. `ERR bad channel`, `ERR level out of range`.

Known issue: the firmware closes a connection that has been idle for 60 s. Above 70 C board temp it turns all channels off by itself and every command replies `ERR overtemp` until it cools down.
