"""orchestrator.reports — orchestrator-side proxy primitive + REST handlers.

Sprint 4 S4.3 introduces this package to host the cookie-session → token-A →
backend pass-through that subsequent slices (S4.4 / S4.5) extend with
``/api/reports/...`` endpoints. Today it only owns ``GET /api/me/leaves``.
"""
