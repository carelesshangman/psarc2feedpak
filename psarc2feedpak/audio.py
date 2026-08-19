"""Wwise .wem and .dds handling via external tools (vgmstream, ffmpeg)."""

import shutil
import subprocess
import sys
from pathlib import Path


def _search_dirs():
    """Where to look for bundled tools, most-specific first.

    Covers running from source and running as a PyInstaller build, where the
    binaries sit next to the .exe (or in its _MEIPASS temp dir) under tools/.
    """
    roots = []
    if getattr(sys, "frozen", False):
        roots += [Path(sys.executable).resolve().parent,
                  Path(getattr(sys, "_MEIPASS", "."))]
    roots.append(Path(__file__).resolve().parent.parent)  # repo root from source
    dirs = []
    for r in roots:
        dirs += [r / "tools" / "vgmstream", r / "tools", r]
    return dirs


def find(name, extra_dirs=()):
    for d in list(extra_dirs) + _search_dirs():
        for candidate in (Path(d) / name, Path(d) / f"{name}.exe"):
            if candidate.is_file():
                return str(candidate)
    return shutil.which(name)


# Keep the child tools from flashing a console window under a windowed GUI build.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed:\n{p.stderr[-800:]}")


def _ffmpeg_encoders(ffmpeg):
    if not ffmpeg:
        return set()
    p = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
    )
    if p.returncode != 0:
        return set()
    encoders = set()
    for line in p.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].startswith("A"):
            encoders.add(fields[1])
    return encoders


class Tools:
    """Bundles the external binaries so callers fail early if they're missing."""

    def __init__(self):
        self.vgmstream = find("vgmstream-cli")
        self.ffmpeg = find("ffmpeg")
        encoders = _ffmpeg_encoders(self.ffmpeg)
        if "libvorbis" in encoders:
            self.vorbis_encoder = "libvorbis"
        elif "vorbis" in encoders:
            self.vorbis_encoder = "vorbis"
        else:
            self.vorbis_encoder = None

    @property
    def can_audio(self):
        return bool(self.vgmstream and self.ffmpeg and self.vorbis_encoder)

    def missing(self):
        out = []
        if not self.vgmstream:
            out.append("vgmstream-cli")
        if not self.ffmpeg:
            out.append("ffmpeg")
        elif not self.vorbis_encoder:
            out.append("ffmpeg with a Vorbis encoder")
        return out

    def wem_to_ogg(self, wem_bytes, out_ogg, scratch, quality=5):
        wem = scratch / "_a.wem"
        wav = scratch / "_a.wav"
        wem.write_bytes(wem_bytes)
        _run([self.vgmstream, "-o", str(wav), str(wem)])
        cmd = [self.ffmpeg, "-y", "-i", str(wav)]
        if self.vorbis_encoder == "vorbis":
            cmd += ["-strict", "-2", "-ac", "2"]
        cmd += ["-c:a", self.vorbis_encoder, "-q:a", str(quality), str(out_ogg)]
        _run(cmd)
        wem.unlink()
        wav.unlink()

    def dds_to_png(self, dds_bytes, out_png, scratch):
        dds = scratch / "_c.dds"
        dds.write_bytes(dds_bytes)
        _run([self.ffmpeg, "-y", "-i", str(dds), str(out_png)])
        dds.unlink()
