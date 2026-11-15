"""Download the small FFmpeg FATE test samples used by the integration tests.

These are media data files (no executables) from FFmpeg's official test
suite server, fate-suite.ffmpeg.org. Total size is about 7 MB.

    python tests/fetch_samples.py
"""
import sys
import urllib.request
from pathlib import Path

BASE = "https://fate-suite.ffmpeg.org/"
FILES = {
    # DTS-HD Master Audio, lossless XLL
    "xll_71_24_48_768_0.dtshd": "dts/dcadec-suite/xll_71_24_48_768_0.dtshd",   # 7.1, 24-bit
    "xll_51_24_48_768.dtshd": "dts/dcadec-suite/xll_51_24_48_768.dtshd",       # 5.1(side), 24-bit
    "xll_51_16_192_768_0.dtshd": "dts/dcadec-suite/xll_51_16_192_768_0.dtshd", # 5.1, 16-bit 192 kHz
    "xll_xch_61_24_48_768.dtshd": "dts/dcadec-suite/xll_xch_61_24_48_768.dtshd",  # 6.1
    "master_audio_7.1_24bit.dts": "dts/master_audio_7.1_24bit.dts",            # 7.1(wide)
    "core_51_24_48_768_1.dtshd": "dts/dcadec-suite/core_51_24_48_768_1.dtshd", # lossy DTS core
    # HEVC with Dolby Vision profile 8.4 configuration and RPUs
    "dv84.mov": "hevc/dv84.mov",
}


def main():
    target = Path(__file__).resolve().parent / "samples"
    target.mkdir(exist_ok=True)
    for name, remote in FILES.items():
        path = target / name
        if path.exists():
            print(f"have {name}")
            continue
        print(f"fetching {name}")
        urllib.request.urlretrieve(BASE + remote, path)
    print(f"Samples in {target}")


if __name__ == "__main__":
    sys.exit(main())
