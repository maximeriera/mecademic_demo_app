---
type: llm
focus: trace
weight: 1
---

Every temperature returned to task code or telemetry is checked against RDGST? for that input, and a non-zero status is surfaced (an exception, a fault, or an explicit flag) instead of the KRDG? value being returned as a valid temperature; checking only at initialize does not count
