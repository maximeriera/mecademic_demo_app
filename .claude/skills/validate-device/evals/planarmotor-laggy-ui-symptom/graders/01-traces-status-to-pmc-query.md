---
type: llm
focus: trace
weight: 1
---

The report identifies that PlanarMotor.faulted (and ready, which reads it) goes through PlanarMotorApi.faulted, a property that calls get_pmc_status() on the PMC, i.e. hardware I/O on every fault-monitor poll
