from fastapi import APIRouter, Response

from tickfast.metrics import render_metrics

router = APIRouter(tags=["metrics"])


@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    body, content_type = render_metrics()
    return Response(content=body, headers={"Content-Type": content_type})
