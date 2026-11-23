---
type: llm
focus: trace
weight: 1
---

The firmware's 60 s idle disconnect is handled explicitly (a keepalive, or reconnecting before the next command after an idle period) without resending a command that may already have reached the board (such as STROBE), and the choice is explained in the code or the report
