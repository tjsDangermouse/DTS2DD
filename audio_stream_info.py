"""Data model for audio stream information."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class AudioStreamInfo:
    """Represents information about an audio stream in a media file.

    The first five fields are the original model used by the AC-3/E-AC-3
    workflow. The remaining fields are filled in by structured (ffprobe JSON)
    probing and are ``None`` when only the legacy text probe was available.
    """
    stream_index: int
    codec_name: str
    channels: str
    bitrate: Optional[str]
    language: Optional[str] = None

    # Structured probe details
    audio_index: Optional[int] = None      # position among audio streams (the N in "a:N")
    format_name: Optional[str] = None      # e.g. "DTS-HD Master Audio", "Unknown"
    profile: Optional[str] = None          # raw ffprobe profile string
    channel_count: Optional[int] = None
    channel_layout: Optional[str] = None   # FFmpeg layout name, e.g. "7.1", "5.1(side)"
    sample_rate: Optional[int] = None
    sample_fmt: Optional[str] = None       # decoder output format, e.g. "s32p"
    bit_depth: Optional[int] = None
    title: Optional[str] = None
    is_default: bool = False
    is_forced: bool = False

    def __str__(self) -> str:
        """Return a formatted string representation of the audio stream."""
        lang_str = f" [{self.language}]" if self.language else ""
        bitrate_str = f" ({self.bitrate})" if self.bitrate else ""
        if self.format_name is None:
            return f"Stream #{self.stream_index}: {self.codec_name} - {self.channels}{bitrate_str}{lang_str}"

        details = []
        if self.channel_count:
            details.append(f"{self.channel_count} ch")
        if self.sample_rate:
            details.append(f"{self.sample_rate} Hz")
        if self.bit_depth:
            details.append(f"{self.bit_depth}-bit")
        detail_str = f" ({', '.join(details)})" if details else ""
        flags = []
        if self.is_default:
            flags.append("default")
        if self.is_forced:
            flags.append("forced")
        flag_str = f" {{{', '.join(flags)}}}" if flags else ""
        title_str = f' "{self.title}"' if self.title else ""
        return (
            f"Stream #{self.stream_index}: {self.format_name} - {self.channels}"
            f"{detail_str}{bitrate_str}{lang_str}{title_str}{flag_str}"
        )
