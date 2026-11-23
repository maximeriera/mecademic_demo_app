# Lake Shore 336 - notes from the manual (ch. 6), double check

- Ethernet, TCP port 7777
- commands end with CR LF, replies end with CR LF
- queries end with `?` and get exactly one reply line
- setters get NO reply at all
- a bad command gets no reply either (so a query with a typo just times out)

Commands we need:

| command | reply | what |
|---|---|---|
| `*IDN?` | `LSCI,MODEL336,LSA1234/#######,2.9` | manufacturer, model, serial/option, firmware |
| `KRDG? A` | `+293.150` | temperature of input A in kelvin (B, C, D the same) |
| `RDGST? A` | `0` | reading status of input A, integer. 0 = fine. bits: 1 invalid reading, 16 temp underrange, 32 temp overrange, 64 sensor units zero, 128 sensor units overrange |
| `SETP 1,300.0` | (none) | setpoint of control loop 1, kelvin |
| `SETP? 1` | `+300.000` | read back the setpoint |
| `RANGE 1,3` | (none) | heater range of output 1: 0 off, 1 low, 2 medium, 3 high |
| `RANGE? 1` | `3` | read back heater range |

On our unit only inputs A and B have sensors.
