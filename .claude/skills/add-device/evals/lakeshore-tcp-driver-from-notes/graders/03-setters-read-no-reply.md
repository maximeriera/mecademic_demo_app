---
type: llm
focus: trace
weight: 1
---

The code never waits for a reply to SETP or RANGE themselves, as the notes say setters get none (sending them bare, or chained with a status query such as ;*ESR? whose one reply is read, both qualify), while KRDG?, RDGST? and SETP? read exactly one reply line
