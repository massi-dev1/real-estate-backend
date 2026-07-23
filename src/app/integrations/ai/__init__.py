"""AI provider seam (§8.18, §5 ``integrations/``).

Everything AI-specific is isolated here so no ``modules/`` package imports a
model SDK directly — the API surface stays stable while the implementation
improves behind this boundary.
"""
