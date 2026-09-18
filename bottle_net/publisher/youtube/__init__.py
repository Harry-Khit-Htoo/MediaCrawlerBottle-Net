"""YouTube integration (official YouTube Data API v3)."""

from bottle_net.publisher.youtube.auth import YouTubeAuth
from bottle_net.publisher.youtube.uploader import YouTubeUploader

__all__ = ["YouTubeAuth", "YouTubeUploader"]
