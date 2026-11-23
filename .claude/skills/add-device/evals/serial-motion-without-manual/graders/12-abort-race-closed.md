---
type: llm
focus: trace
weight: 1
---

An abort() that arrives while the move command is being sent, before the wait starts polling, still makes the move raise instead of reporting success (for example the abort state is captured before the move command is sent)
