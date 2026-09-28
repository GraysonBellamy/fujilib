"""The register map and everything it is described in terms of (design §5).

Submodules:

- :mod:`~fujilib.registry.channels` — channel identifiers, roles, gases, label sources.
- :mod:`~fujilib.registry.units` — measurement units and the unit-code register.
- :mod:`~fujilib.registry.enums` — the enumerated register values of the manual.
- :mod:`~fujilib.registry.regions` — valid read regions per function code.
- :mod:`~fujilib.registry.write_policy` — the frozen write envelope and operations.
- :mod:`~fujilib.registry.registers` — :class:`RegisterSpec` and the full map.
- :mod:`~fujilib.registry.typecode` — the type-code decoder and channel layout.
"""
