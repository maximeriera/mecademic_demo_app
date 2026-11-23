---
tags: [skill-creator]
max_turns: 120
timeout_seconds: 2400
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

for the gige_cv_analysis example workspace I want the app to control our ring light. it's an ESP32 board we flashed ourselves, protocol notes in ringlight_protocol.md. it's only for that demo so dont put it in the core devices/registry stuff, keep the driver in the workspace. task code needs to set the brightness per channel and fire a strobe right before grabbing a frame.

Input files: ringlight_protocol.md
