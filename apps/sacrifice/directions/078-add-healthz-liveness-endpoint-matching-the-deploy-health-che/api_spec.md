# API spec

GET /healthz
  Response 200: {"status": "ok"}
  No authentication required (liveness probe).
  Reuses the existing /api/health handler logic.
