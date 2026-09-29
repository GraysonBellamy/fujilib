---
description: fujilib.devices — the analyzer facade, its session, discovery, data models, decoders and read procedures.
---

# `fujilib.devices`

The `Analyzer` facade and `open_device` (design §7.1, §7.2), the session every
call goes through (design §6), discovery (design §7.5) and device profiles; the
frozen data models (design §8), the pure decoders from register banks to
models, the read procedures that join a read plan to a decoder, safety tiers
and capability flags, and the unified-API snapshots; and the write side:
encoding a setting, writing it and reading it back, settings documents, and
the operation commands (design §6.1-§6.4, [Safety](../safety.md)); and the
front panel's manual calibrations, planned and watched (design §6.5).

## Opening and using an analyzer

::: fujilib.devices.factory

::: fujilib.devices.analyzer

::: fujilib.devices.session

::: fujilib.devices.discovery

::: fujilib.devices.profile

## Models

::: fujilib.devices.models

::: fujilib.devices.capability

::: fujilib.devices.snapshot

## Decoding and reading

::: fujilib.devices.decode

::: fujilib.devices.reads

## Writing and commands

::: fujilib.devices.encode

::: fujilib.devices.writes

::: fujilib.devices.settings

::: fujilib.devices.operations

## The front panel

::: fujilib.devices.panel
