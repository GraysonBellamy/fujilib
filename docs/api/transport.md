---
description: fujilib.transport — the transport contract, serial settings, the serial transport and the scripted fake.
---

# `fujilib.transport`

A transport is a thin lifecycle object that exposes the byte stream the Modbus
bus binds to: the real `anyserial.SerialPort`, so `anymodbus` keeps its
drain-after-send and input-reset behaviour (design §4.1).

::: fujilib.transport.base

::: fujilib.transport.serial

::: fujilib.transport.fake
