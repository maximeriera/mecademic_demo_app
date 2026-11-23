---
tags: [skill-creator]
max_turns: 120
timeout_seconds: 2400
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

we got a Lake Shore 336 temperature controller for the thermal test station and the cell needs to talk to it over ethernet. I wrote down the commands we need in lakeshore_notes.md. task code has to read the temperature of inputs A and B and change the setpoint of loop 1, and we want to see the temps on the devices tab. call the type lakeshore_336

Input files: lakeshore_notes.md
