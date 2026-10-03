"""Authenticated reference library routes using the independently installed tools."""
from fastapi import APIRouter
from fastapi.responses import FileResponse


def create_wool_library_router(get_service):
    router = APIRouter(prefix="/api/wool-library")

    @router.get("")
    def snapshot():
        return get_service().wool_library().snapshot()

    @router.get("/images/{identity}")
    def image(identity: str):
        return FileResponse(get_service().wool_library().image(identity))

    @router.post("/{action}")
    def action(action: str, body: dict):
        return get_service().wool_library_action(action, body)

    return router
