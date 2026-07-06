"""Data model for audio stream information."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class AudioStreamInfo:
    """Represents information about an audio stream in a media file."""
    stream_index: int
    codec_name: str
    channels: str
    bitrate: Optional[str]
    language: Optional[str] = None

    def __str__(self) -> str:
        """Return a formatted string representation of the audio stream."""
        lang_str = f" [{self.language}]" if self.language else ""
        bitrate_str = f" ({self.bitrate})" if self.bitrate else ""
        return f"Stream #{self.stream_index}: {self.codec_name} - {self.channels}{bitrate_str}{lang_str}"
