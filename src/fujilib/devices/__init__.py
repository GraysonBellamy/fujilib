"""The analyzer facade, its session, discovery, and the device-level models.

Kept free of eager imports: :mod:`fujilib.registry` imports
:mod:`fujilib.devices.capability`, so this package must not import anything
that imports the registry back. Import the submodules, or the top-level
:mod:`fujilib` names.
"""
