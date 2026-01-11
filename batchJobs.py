
# =================================================================================================
# backend/BatchJobs.py
# PixelPerfect Batch Screenshot Router Alias
# =================================================================================================
"""
Drop-in router alias for batch screenshot endpoints.

Why this file? Some projects prefer importing `BatchJobs` instead of `batch`.
This module simply re-exports the existing FastAPI router from `batch.py` so
`main.py` can do either of the following:

    from BatchJobs import router as batch_router
    # or
    from routers.batch import router as batch_router

Both give you the same endpoints:
  POST   /api/v1/batch/submit          - Submit batch screenshot job
  GET    /api/v1/batch/jobs             - List all batch jobs
  GET    /api/v1/batch/jobs/{job_id}    - Get job details
  POST   /api/v1/batch/jobs/{job_id}/retry_failed - Retry failed screenshots
  DELETE /api/v1/batch/jobs/{job_id}    - Delete batch job

Converted from YCD's BatchJobs.py for PixelPerfect Screenshot API.
"""

from routers.batch import router as router

__all__ = ["router"]

# ============= End BatchJobs Alias =============