---
type: llm
focus: trace
weight: 1
---

Task code can set a channel's brightness and fire a strobe, and the code or report deals with STROBE replying only after the flash ends, so a blocking strobe followed by a grab captures an unlit frame (for example a strobe that returns before the OK so the grab overlaps the flash, ON/grab/OFF, or a hardware trigger); the read timeout covers the 500 ms maximum strobe
