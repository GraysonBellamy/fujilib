---
description: fujilib.protocol — the register-word codec, the read planner, the Modbus port and client, and the error map.
---

# `fujilib.protocol`

The Modbus layer (design §4): the codec and planner are pure; the port owns one
bus per serial line, and the client of one station moves words with retries,
counters and timing.

::: fujilib.protocol.base

::: fujilib.protocol.modbus.codec

::: fujilib.protocol.modbus.read_plan

::: fujilib.protocol.modbus.port

::: fujilib.protocol.modbus.client

::: fujilib.protocol.modbus.errors
