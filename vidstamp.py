#!/usr/bin/env python3
"""
vidstamp — Burn real-world timestamps into pre-recorded video.

Each frame is labelled with the exact wall-clock time it was captured,
derived from a user-supplied start time and the video's native frame rate.
Timestamps are accurate to one frame interval (millisecond precision).
Recordings that span midnight are handled automatically.

Requires FFmpeg (ffmpeg + ffprobe) on PATH.
"""

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import click

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# (x, y) expressions understood by FFmpeg's drawtext filter.
# tw/th = rendered text width/height; w/h = frame width/height.
POSITIONS: dict[str, tuple[str, str]] = {
    "top-left":      ("10",           "10"),
    "top-right":     ("w-tw-10",      "10"),
    "bottom-left":   ("10",           "h-th-10"),
    "bottom-right":  ("w-tw-10",      "h-th-10"),
    "top-center":    ("(w-tw)/2",     "10"),
    "bottom-center": ("(w-tw)/2",     "h-th-10"),
}

# Accepted input datetime formats (most → least specific).
TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
)

X264_PRESETS = [
    "ultrafast", "superfast", "veryfast", "faster",
    "fast", "medium", "slow", "slower", "veryslow",
]

# Hardware encoder codec names → quality flags.
# The sentinel "{crf}" is replaced with the --crf value at runtime.
# Encoders not listed here get no quality flags (FFmpeg uses its default).
HW_QUALITY_FLAGS: dict[str, list[str]] = {
    # NVIDIA NVENC
    "h264_nvenc":        ["-rc", "vbr", "-cq", "{crf}"],
    "hevc_nvenc":        ["-rc", "vbr", "-cq", "{crf}"],
    "av1_nvenc":         ["-rc", "vbr", "-cq", "{crf}"],
    # Intel Quick Sync
    "h264_qsv":          ["-global_quality", "{crf}"],
    "hevc_qsv":          ["-global_quality", "{crf}"],
    "av1_qsv":           ["-global_quality", "{crf}"],
    # VA-API  (Intel / AMD on Linux)
    "h264_vaapi":        ["-qp", "{crf}"],
    "hevc_vaapi":        ["-qp", "{crf}"],
    "av1_vaapi":         ["-qp", "{crf}"],
    # AMD AMF
    "h264_amf":          ["-quality", "balanced", "-qp_i", "{crf}", "-qp_p", "{crf}"],
    "hevc_amf":          ["-quality", "balanced", "-qp_i", "{crf}", "-qp_p", "{crf}"],
    "av1_amf":           ["-quality", "balanced", "-qp_i", "{crf}", "-qp_p", "{crf}"],
    # Apple VideoToolbox (macOS)
    "h264_videotoolbox": ["-q:v", "65"],
    "hevc_videotoolbox": ["-q:v", "65"],
}


# ─────────────────────────────────────────────────────────────────────────────
# Video probing
# ─────────────────────────────────────────────────────────────────────────────

def probe(path: str) -> dict:
    """Run ffprobe on *path* and return its parsed JSON output."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "ffprobe returned a non-zero exit code.")
    return json.loads(r.stdout)


def video_stream(data: dict) -> dict:
    """Return the first video stream dict from *data*, or raise."""
    for s in data["streams"]:
        if s["codec_type"] == "video":
            return s
    raise RuntimeError("No video stream found in the file.")


# ─────────────────────────────────────────────────────────────────────────────
# FFmpeg runner
# ─────────────────────────────────────────────────────────────────────────────

def run_ffmpeg(cmd: list, total_frames: int, verbose: bool) -> tuple:
    """
    Run FFmpeg, returning (returncode, stderr_text).

    In verbose mode stderr is streamed in real time; each progress line
    (`frame=…`) is parsed and rendered as an in-place progress bar.
    In quiet mode stderr is simply captured.
    """
    if not verbose:
        r = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
        return r.returncode, r.stderr

    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, bufsize=1)
    stderr_lines: list = []
    buf = ""
    total_str = str(total_frames)
    bar_w = 28

    while True:
        chunk = proc.stderr.read(256)
        if not chunk:
            break
        buf += chunk
        parts = re.split(r"[\r\n]", buf)
        buf = parts[-1]          # keep any incomplete trailing fragment
        for line in parts[:-1]:
            line = line.strip()
            if not line:
                continue
            stderr_lines.append(line)
            m = re.match(r"frame=\s*(\d+)", line)
            if not m:
                continue
            frame   = int(m.group(1))
            pct     = min(100.0, frame / total_frames * 100) if total_frames else 0.0
            fps_m   = re.search(r"fps=\s*([\d.]+)", line)
            speed_m = re.search(r"speed=\s*([\d.]+x)", line)
            fps_val   = fps_m.group(1)   if fps_m   else "?"
            speed_val = speed_m.group(1) if speed_m else "?"
            filled = int(bar_w * pct / 100)
            bar    = "█" * filled + "░" * (bar_w - filled)
            click.echo(
                f"\r  [{bar}] {pct:5.1f}%"
                f"  frame {frame:{len(total_str)}}/{total_str}"
                f"  {fps_val} fps  {speed_val}   ",
                nl=False,
            )

    proc.wait()
    if buf.strip():
        stderr_lines.append(buf.strip())
    click.echo()   # leave the progress line intact
    return proc.returncode, "\n".join(stderr_lines)


# ─────────────────────────────────────────────────────────────────────────────
# Filter-graph construction
# ─────────────────────────────────────────────────────────────────────────────

def day_segments(start_dt: datetime, duration: float) -> list:
    """
    Partition the recording into per-calendar-day segments so the date portion
    of the overlay is always correct, even when a recording spans midnight.

    Returns a list of tuples:
        (date_str, t_start, t_end_or_None, smpte_offset)

    where *smpte_offset* satisfies:
        displayed_seconds_past_midnight = t_video + smpte_offset
    """
    segments = []
    t   = 0.0
    cur = start_dt

    while t < duration:
        midnight = cur.replace(hour=0, minute=0, second=0, microsecond=0)
        next_mid = midnight + timedelta(days=1)

        # Seconds into the current calendar day at video time t.
        day_off   = (cur - midnight).total_seconds()
        smpte_off = day_off - t   # → displayed = t + smpte_off

        secs_left_in_day = (next_mid - cur).total_seconds()

        if t + secs_left_in_day < duration:
            # Midnight falls before the video ends — close this segment there.
            t_end = t + secs_left_in_day
            segments.append((cur.strftime("%Y-%m-%d"), t, t_end, smpte_off))
            t   = t_end
            cur = next_mid
        else:
            # Video ends before the next midnight — final (or only) segment.
            segments.append((cur.strftime("%Y-%m-%d"), t, None, smpte_off))
            break

    return segments


def drawtext_filter(
    date_str:    str,
    t_start:     float,
    t_end,               # float | None
    smpte_off:   float,
    *,
    position:    str,
    font_size:   int,
    font_color:  str,
    box:         bool,
    box_color:   str,
    box_opacity: float,
    font_file,           # str | None
) -> str:
    """
    Build one drawtext filter string for a single calendar-day segment.

    FFmpeg filter-option notes
    --------------------------
    • Inside  text=  values, colons are option separators and must be escaped
      as  \\:  (which appears as  \\\\:  in a Python string literal so that the
      final string delivered to FFmpeg contains  \\: ).
    • Single-quoted values in a filter option are FFmpeg-level quoting; they
      protect commas from being interpreted as filter-chain separators, so
      commas inside  enable='...'  and  text='...'  do not need escaping.
    • %{eif\\:expr\\:d\\:N}  is FFmpeg's integer-formatting expansion:
        eif  = evaluate as integer
        expr = the expression to evaluate
        d    = decimal output
        N    = minimum digit width (zero-padded)
    """
    x, y = POSITIONS[position]
    O    = f"{smpte_off:.6f}"   # numeric constant baked into every expression

    def eif(expr: str, digits: int) -> str:
        """Wrap *expr* in an FFmpeg eif text expansion."""
        return f"%{{eif\\:{expr}\\:d\\:{digits}}}"

    # Time components — all derived from (t + O) = seconds past midnight.
    hh = eif(f"floor((t+{O})/3600)",       2)   # 00-23
    mm = eif(f"floor(mod(t+{O},3600)/60)", 2)   # 00-59
    ss = eif(f"floor(mod(t+{O},60))",      2)   # 00-59
    ms = eif(f"floor(mod(t+{O},1)*1000)",  3)   # 000-999

    # Display separators inside text= also need \\: escaping.
    text = f"{date_str} {hh}\\:{mm}\\:{ss}.{ms}"

    # enable expression — single-quoted so internal commas are safe.
    enable = (
        f"between(t,{t_start:.6f},{t_end:.6f})"
        if t_end is not None
        else f"gte(t,{t_start:.6f})"
    )

    font_part = f"fontfile='{font_file}':" if font_file else ""
    box_part  = (
        f"box=1:boxcolor={box_color}@{box_opacity:.2f}:boxborderw=6:"
        if box else ""
    )

    return (
        f"drawtext="
        f"{font_part}"
        f"fontsize={font_size}:"
        f"fontcolor={font_color}:"
        f"{box_part}"
        f"x={x}:y={y}:"
        f"enable='{enable}':"
        f"text='{text}'"
    )


def build_vf(start_dt: datetime, duration: float, **draw_opts) -> str:
    """
    Return the complete -vf argument string.

    For recordings that stay within one calendar day this is a single drawtext
    filter.  For longer recordings it is multiple drawtext filters comma-chained
    (each with an  enable=  window), one per day boundary crossed.
    """
    segs = day_segments(start_dt, duration)
    return ",".join(
        drawtext_filter(date_str, t_start, t_end, smpte_off, **draw_opts)
        for date_str, t_start, t_end, smpte_off in segs
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("input_file",  type=click.Path(exists=True, dir_okay=False))
@click.argument("output_file", type=click.Path(dir_okay=False))
# ── time ──────────────────────────────────────────────────────────────────────
@click.option("--start-time", "-s",
              required=True, metavar="DATETIME",
              help='Wall-clock time when recording started. '
                   'Format: "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD HH:MM:SS.mmm"')
# ── overlay appearance ────────────────────────────────────────────────────────
@click.option("--position", "-p",
              default="top-left", show_default=True,
              type=click.Choice(list(POSITIONS), case_sensitive=False),
              help="On-screen position of the timestamp.")
@click.option("--font-size", "-fs",
              default=36, show_default=True, type=int,
              help="Font size in points.")
@click.option("--font-color", "-fc",
              default="white", show_default=True,
              help="Font color — CSS name (white, yellow …) or #RRGGBB.")
@click.option("--box/--no-box",
              default=True, show_default=True,
              help="Draw a semi-transparent background box.")
@click.option("--box-color",
              default="black", show_default=True,
              help="Box background color.")
@click.option("--box-opacity",
              default=0.5, show_default=True,
              type=click.FloatRange(0.0, 1.0), metavar="FLOAT",
              help="Box opacity (0.0 = invisible, 1.0 = solid).")
@click.option("--font-file",
              default=None, type=click.Path(exists=True, dir_okay=False),
              help="Path to a custom .ttf/.otf font file.")
# ── encoding ──────────────────────────────────────────────────────────────────
@click.option("--codec", "-c",
              default="libx264", show_default=True,
              help="Output video codec. Use a hardware encoder for faster processing: "
                   "h264_nvenc (NVIDIA), h264_qsv (Intel), h264_vaapi (Intel/AMD on Linux), "
                   "h264_amf (AMD), h264_videotoolbox (macOS). "
                   "Run 'ffmpeg -encoders | grep h264' to see what is available.")
@click.option("--crf",
              default=18, show_default=True, type=int,
              help="Quality level (lower = better quality, larger file). "
                   "Interpreted as CRF for libx264/libx265, or as an equivalent "
                   "QP/CQ value for hardware encoders.")
@click.option("--preset",
              default="medium", show_default=True,
              type=click.Choice(X264_PRESETS),
              help="Encoding speed/quality preset (libx264/libx265 only).")
@click.option("--hwaccel/--no-hwaccel",
              default=False, show_default=True,
              help="Enable hardware-accelerated decoding (-hwaccel auto). "
                   "Pair with a hardware --codec for maximum speed.")
@click.option("--audio/--no-audio",
              default=True, show_default=True,
              help="Copy the audio stream to the output.")
# ── behaviour ─────────────────────────────────────────────────────────────────
@click.option("--verbose", "-v",
              is_flag=True,
              help="Show a real-time encoding progress bar.")
@click.option("--overwrite/--no-overwrite",
              default=False, show_default=True,
              help="Overwrite the output file if it already exists.")
@click.option("--dry-run",
              is_flag=True,
              help="Print the FFmpeg command that would run, then exit.")
@click.version_option("1.0.0", prog_name="vidstamp")
def cli(
    input_file, output_file, start_time,
    position, font_size, font_color, box, box_color, box_opacity, font_file,
    codec, crf, preset, hwaccel, audio, verbose, overwrite, dry_run,
):
    """
    vidstamp — Burn real-world timestamps into a pre-recorded video.

    \b
    Every frame is labelled with the wall-clock date and time at which it was
    captured, calculated from INPUT_FILE's frame rate and the supplied start
    time.  Timestamps are accurate to one frame interval (milliseconds).
    Recordings that cross midnight are handled automatically — the date in
    the overlay updates correctly without any extra configuration.

    \b
    Examples
    ────────
      # Basic usage — start time to the second:
      vidstamp input.mp4 output.mp4 -s "2024-06-01 09:05:30"

    \b
      # Sub-second start time, custom position and style:
      vidstamp input.mp4 output.mp4 -s "2024-06-01 09:05:30.250" \\
               --position bottom-right --font-size 48 --box-opacity 0.7

    \b
      # Custom font, higher quality encode, no audio:
      vidstamp input.mkv output.mkv -s "2024-06-01 23:50:00" \\
               --font-file /usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf \\
               --crf 16 --preset slow --no-audio

    \b
      # Preview the FFmpeg command without encoding:
      vidstamp input.mp4 output.mp4 -s "2024-06-01 09:00:00" --dry-run
    """

    # ── dependency check ──────────────────────────────────────────────────────
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        click.secho(
            f"Error: {', '.join(missing)} not found on PATH.\n"
            "       Install FFmpeg:  https://ffmpeg.org/download.html",
            fg="red", err=True,
        )
        sys.exit(1)

    # ── parse start time ──────────────────────────────────────────────────────
    start_dt = None
    for fmt in TIME_FORMATS:
        try:
            start_dt = datetime.strptime(start_time, fmt)
            break
        except ValueError:
            pass
    if start_dt is None:
        click.secho(
            f'Error: Cannot parse start time "{start_time}".\n'
            "       Expected:  YYYY-MM-DD HH:MM:SS\n"
            "           or:    YYYY-MM-DD HH:MM:SS.mmm",
            fg="red", err=True,
        )
        sys.exit(1)

    # ── output-exists guard ───────────────────────────────────────────────────
    if Path(output_file).exists() and not overwrite and not dry_run:
        click.secho(
            f'Error: "{output_file}" already exists.\n'
            "       Use --overwrite to replace it.",
            fg="red", err=True,
        )
        sys.exit(1)

    # ── probe input ───────────────────────────────────────────────────────────
    click.echo(f'\nProbing  "{input_file}" …')
    try:
        meta = probe(input_file)
        vs   = video_stream(meta)
    except RuntimeError as exc:
        click.secho(f"Error: {exc}", fg="red", err=True)
        sys.exit(1)

    fps_n, fps_d = vs.get("r_frame_rate", "30/1").split("/")
    fps      = float(fps_n) / float(fps_d)
    duration = float(meta["format"].get("duration", 0.0))
    end_dt   = start_dt + timedelta(seconds=duration)

    ms_per_frame = 1000.0 / fps

    click.echo(f'  Resolution  : {vs.get("width","?")}×{vs.get("height","?")}')
    click.echo(f'  Frame rate  : {fps:.4f} fps  ({ms_per_frame:.3f} ms / frame)')
    click.echo(f'  Duration    : {timedelta(seconds=int(duration))}  ({duration:.3f} s)')
    click.echo(f'  Input codec : {vs.get("codec_name","?")}')
    click.echo(f'  Starts      : {start_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}')
    click.echo(f'  Ends        : {end_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}')

    if end_dt.date() != start_dt.date():
        n_midnights = (end_dt.date() - start_dt.date()).days
        click.secho(
            f"  ⚠  Recording spans {n_midnights} midnight(s) — "
            "date overlay updates automatically.",
            fg="yellow",
        )

    # ── build -vf filter graph ────────────────────────────────────────────────
    vf = build_vf(
        start_dt, duration,
        position=position,
        font_size=font_size,
        font_color=font_color,
        box=box,
        box_color=box_color,
        box_opacity=box_opacity,
        font_file=font_file,
    )

    # ── VA-API: upload software frames to a hardware surface for the encoder ──
    # drawtext is a software filter, so decoded frames are always in software
    # format by the time they reach the encoder. VA-API requires frames on a
    # hardware surface, so we append an explicit format conversion and upload.
    if codec.endswith("_vaapi"):
        vf = f"{vf},format=nv12,hwupload"

    # ── assemble FFmpeg command ────────────────────────────────────────────────
    cmd = ["ffmpeg"]

    if codec.endswith("_vaapi"):
        # VA-API's hwupload filter requires an explicit device reference.
        # Decoding stays in software so the drawtext filter can operate on
        # normal frames; only encoding uses the VA-API hardware.
        cmd += [
            "-init_hw_device", "vaapi=va:/dev/dri/renderD128",
            "-filter_hw_device", "va",
        ]
        if hwaccel:
            click.secho(
                "Note: --hwaccel is ignored for VA-API encoders — software "
                "decoding is required for the drawtext filter. "
                "Hardware encoding is still active.",
                fg="yellow",
            )
    elif hwaccel:
        cmd += ["-hwaccel", "auto"]

    cmd += [
        "-y" if overwrite else "-n",
        "-i", input_file,
        "-vf", vf,
        "-c:v", codec,
    ]

    if codec in ("libx264", "libx265"):
        cmd += ["-crf", str(crf), "-preset", preset]
    elif codec in HW_QUALITY_FLAGS:
        cmd += [f.replace("{crf}", str(crf)) for f in HW_QUALITY_FLAGS[codec]]

    cmd += ["-c:a", "copy"] if audio else ["-an"]
    cmd.append(output_file)

    # ── dry-run: print command and exit ───────────────────────────────────────
    if dry_run:
        click.echo("\nDry-run — FFmpeg command that would be executed:\n")
        lines = []
        i = 0
        while i < len(cmd):
            arg = cmd[i]
            if arg == "-vf":
                # Print the long -vf value indented on its own line.
                lines.append(f"  -vf \\\n    '{cmd[i+1]}'")
                i += 2
            else:
                lines.append(f"  {arg!r}" if " " in arg else f"  {arg}")
                i += 1
        click.echo(" \\\n".join(lines))
        return

    # ── encode ────────────────────────────────────────────────────────────────
    total_frames = round(fps * duration)
    click.echo(f'\nEncoding  → "{output_file}" …')
    if not verbose:
        click.echo("  (FFmpeg is running; this may take a while. Use --verbose for progress.)\n")

    returncode, stderr = run_ffmpeg(cmd, total_frames, verbose)

    if returncode != 0:
        click.secho("FFmpeg reported an error:", fg="red", err=True)
        # Show the last 25 lines — FFmpeg's progress lines dominate stderr.
        tail = stderr.strip().splitlines()[-25:]
        click.echo("\n".join(tail), err=True)
        sys.exit(1)

    click.secho("\n✓  Encoding complete!", fg="green", bold=True)

    # ── verify output ─────────────────────────────────────────────────────────
    try:
        out_meta   = probe(output_file)
        out_vs     = video_stream(out_meta)
        out_dur    = float(out_meta["format"].get("duration", 0))
        out_size   = Path(output_file).stat().st_size / (1024 * 1024)
        click.echo(
            f"  Output  : {out_vs['width']}×{out_vs['height']}, "
            f"{out_vs.get('codec_name','?')}, "
            f"{timedelta(seconds=int(out_dur))}, "
            f"{out_size:.1f} MB"
        )
    except Exception:
        pass   # verification is informational only


if __name__ == "__main__":
    cli()
