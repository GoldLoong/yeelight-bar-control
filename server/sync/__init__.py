"""Screen sync for IINA and browser sources."""

from .capture import (
    DisplayInfo,
    find_media_window,
    invalidate_window_cache,
    list_displays,
    SampleColor,
    sample_region_color,
)
from .engine import SyncEngine, SyncRuntimeState
from .iina import (
    IinaStatus,
    consume_seek,
    get_iina_status,
    is_iina_running,
    status_to_dict,
    update_plugin_status,
)
from .media import (
    MediaStatus,
    consume_browser_seek,
    get_browser_status,
    media_status_to_dict,
    update_browser_status,
)

__all__ = [
    "DisplayInfo",
    "IinaStatus",
    "MediaStatus",
    "SampleColor",
    "SyncEngine",
    "SyncRuntimeState",
    "consume_browser_seek",
    "consume_seek",
    "find_media_window",
    "get_browser_status",
    "get_iina_status",
    "invalidate_window_cache",
    "is_iina_running",
    "list_displays",
    "media_status_to_dict",
    "sample_region_color",
    "status_to_dict",
    "update_browser_status",
    "update_plugin_status",
]
