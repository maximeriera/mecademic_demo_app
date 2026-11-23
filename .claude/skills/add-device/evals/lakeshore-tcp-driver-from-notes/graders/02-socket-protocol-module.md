---
type: llm
focus: trace
weight: 1
---

A protocol module in devices/api/ talks plain TCP (port 7777 by default, CR LF terminators), serializes every exchange with a lock, and closes the session when a socket error occurs
