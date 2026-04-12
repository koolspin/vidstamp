# vidstamp

Burn real-world timestamps into pre-recorded video.

Every frame is labelled with the exact wall-clock date and time at which it
was captured, derived from a user-supplied start time and the video's native
frame rate. Timestamps are accurate to one frame interval (millisecond
precision). Recordings that cross midnight are handled automatically — the
date in the overlay advances correctly without any extra configuration.

---

## Requirements

- **Python 3.8+**
- **FFmpeg** (includes both `ffmpeg` and `ffprobe`) installed and on your PATH

### Installing FFmpeg

**Ubuntu / Debian**
```bash
sudo apt update && sudo apt install ffmpeg
```

**Fedora / RHEL**
```bash
sudo dnf install ffmpeg
```

**macOS (Homebrew)**
```bash
brew install ffmpeg
```

---

## Installation

### Option A — `pipx` (recommended, installs in an isolated environment)
```bash
pipx install .
```

### Option B — `pip` (system or virtualenv)
```bash
pip install .
```

### Option C — run directly (no install)
```bash
pip install click        # only dependency
python vidstamp.py --help
```

---

## Usage

```
vidstamp [OPTIONS] INPUT_FILE OUTPUT_FILE
```

### Required argument

| Option | Description |
|--------|-------------|
| `-s`, `--start-time DATETIME` | Wall-clock time when the recording started.<br>Format: `"YYYY-MM-DD HH:MM:SS"` or `"YYYY-MM-DD HH:MM:SS.mmm"` |

### Overlay appearance

| Option | Default | Description |
|--------|---------|-------------|
| `-p`, `--position` | `top-left` | Where the timestamp appears on screen.<br>Choices: `top-left`, `top-right`, `bottom-left`, `bottom-right`, `top-center`, `bottom-center` |
| `-fs`, `--font-size` | `36` | Font size in points |
| `-fc`, `--font-color` | `white` | Font color — CSS name (`white`, `yellow`, `lime`) or `#RRGGBB` |
| `--box` / `--no-box` | on | Draw a semi-transparent box behind the text |
| `--box-color` | `black` | Box background color |
| `--box-opacity` | `0.5` | Box opacity (0.0 = invisible, 1.0 = solid) |
| `--font-file PATH` | — | Path to a custom `.ttf` or `.otf` font file |

### Encoding

| Option | Default | Description |
|--------|---------|-------------|
| `-c`, `--codec` | `libx264` | Output video codec. See [Hardware encoding](#hardware-encoding) for faster alternatives |
| `--crf` | `18` | Quality factor. Lower = better quality & larger file. Visually lossless ≈ 18, good quality ≈ 23. Applied as CRF for `libx264`/`libx265`, or as an equivalent QP/CQ value for hardware encoders |
| `--preset` | `medium` | Encoding speed/quality preset (`ultrafast` → `veryslow`). Slower presets produce smaller files at the same CRF. Applies to `libx264`/`libx265` only |
| `--hwaccel` / `--no-hwaccel` | off | Enable hardware-accelerated **decoding** (`-hwaccel auto`). Pair with a hardware `--codec` for maximum speed |
| `--audio` / `--no-audio` | on | Copy the audio stream to the output |

### Behaviour

| Option | Default | Description |
|--------|---------|-------------|
| `-v`, `--verbose` | off | Show a real-time encoding progress bar |
| `--overwrite` / `--no-overwrite` | off | Overwrite the output file if it already exists |
| `--dry-run` | — | Print the FFmpeg command that would run, then exit without encoding |
| `-h`, `--help` | — | Show help and exit |
| `--version` | — | Show version and exit |

---

## Examples

### Basic — start time to the second
```bash
vidstamp input.mp4 output.mp4 -s "2024-06-01 09:05:30"
```

### Sub-second start time, bottom-right, larger font
```bash
vidstamp input.mp4 output.mp4 \
  -s "2024-06-01 09:05:30.250" \
  --position bottom-right \
  --font-size 48 \
  --box-opacity 0.7
```

### Custom monospace font (sharper digits)
```bash
vidstamp input.mkv output.mkv \
  -s "2024-06-01 23:50:00" \
  --font-file /usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf
```

### Higher-quality encode, slower preset
```bash
vidstamp input.mp4 output.mp4 \
  -s "2024-06-01 09:00:00" \
  --crf 16 --preset slow
```

### HEVC output (smaller file, same quality)
```bash
vidstamp input.mp4 output.mp4 \
  -s "2024-06-01 09:00:00" \
  --codec libx265 --crf 22
```

### No background box, yellow text
```bash
vidstamp input.mp4 output.mp4 \
  -s "2024-06-01 09:00:00" \
  --no-box --font-color yellow
```

### Show a progress bar during encoding
```bash
vidstamp input.mp4 output.mp4 -s "2024-06-01 09:00:00" --verbose
```

### Preview the FFmpeg command without encoding
```bash
vidstamp input.mp4 output.mp4 -s "2024-06-01 09:00:00" --dry-run
```

### Hardware-accelerated encoding (NVIDIA)
```bash
vidstamp input.mp4 output.mp4 \
  -s "2024-06-01 09:00:00" \
  --codec h264_nvenc --hwaccel --verbose
```

### Hardware-accelerated encoding (Intel / AMD on Linux)
```bash
vidstamp input.mp4 output.mp4 \
  -s "2024-06-01 09:00:00" \
  --codec h264_vaapi --hwaccel --verbose
```

---

## Hardware encoding

Software encoding with `libx264` is CPU-bound and can be slow for long or
high-resolution videos. Most modern systems have a dedicated hardware encoder
that is significantly faster.

### Discovering available encoders

```bash
ffmpeg -encoders 2>/dev/null | grep -E 'h264|hevc|av1'
```

### Codec names by vendor

| Vendor | H.264 codec | H.265/HEVC codec | AV1 codec |
|--------|-------------|------------------|-----------|
| NVIDIA (NVENC) | `h264_nvenc` | `hevc_nvenc` | `av1_nvenc` |
| Intel (Quick Sync) | `h264_qsv` | `hevc_qsv` | `av1_qsv` |
| Intel / AMD — Linux (VA-API) | `h264_vaapi` | `hevc_vaapi` | `av1_vaapi` |
| AMD (AMF) | `h264_amf` | `hevc_amf` | `av1_amf` |
| Apple (VideoToolbox) | `h264_videotoolbox` | `hevc_videotoolbox` | — |

### Quality with hardware encoders

Hardware encoders do not use the same `-crf` scale as `libx264`. vidstamp
maps `--crf` to the closest equivalent for each encoder:

| Encoder family | Quality parameter used |
|---|---|
| NVENC | `-rc vbr -cq <crf>` |
| Quick Sync | `-global_quality <crf>` |
| VA-API | `-qp <crf>` |
| AMF | `-qp_i <crf> -qp_p <crf>` |
| VideoToolbox | `-q:v 65` (fixed; `--crf` is ignored) |

The default `--crf 18` is a reasonable starting point for all encoders, but
hardware encoders produce visually lossless output at slightly higher values
(e.g. `--crf 20`). Use `--dry-run` to inspect the exact FFmpeg command before
committing to a long encode.

### `--hwaccel`

The `--hwaccel` flag adds `-hwaccel auto` to the FFmpeg command, which enables
hardware-accelerated **decoding** of the input file in addition to hardware
encoding of the output. This reduces CPU usage further and can improve
throughput on long files. It is safe to use with any `--codec`.

---

## How it works

1. **Probe** — `ffprobe` extracts the frame rate, duration, resolution, and
   codec from the input file.

2. **Segment** — the recording is split into per-calendar-day windows. A
   single recording that starts at 23:50 and runs for 20 minutes gets two
   windows: one for the first day and one for the next. Each window carries
   its own date string and a time offset.

3. **Filter graph** — one `drawtext` filter is produced per window, each
   gated by an `enable='between(t,…,…)'` expression. The time digits are
   rendered using FFmpeg's `%{eif:…:d:N}` expansion evaluated against the
   frame's presentation timestamp (`t`), so the value updates per-frame and
   is correct for any frame rate.

   ```
   displayed_time = t_video + smpte_offset
   ```

   where `smpte_offset = seconds_into_day_at_recording_start - 0`.

4. **Encode** — FFmpeg re-encodes the video with the overlay burned in.
   Audio is stream-copied (no re-encode) unless `--no-audio` is supplied.

5. **Verify** — `ffprobe` is run on the output to confirm it was written
   successfully and to display a summary.

---

## Timestamp accuracy

The displayed timestamp advances by exactly `1 / fps` seconds per frame —
the same interval as the actual capture hardware. For a 30 fps recording
this is `33.333 ms` per step; for 60 fps it is `16.667 ms`. Sub-millisecond
accuracy beyond frame boundaries is not possible from the video itself
without an embedded timecode track.

---

## Tips

- A **monospace font** (`DejaVuSansMono`, `Courier`, `Roboto Mono`) prevents
  the timestamp from jumping left and right as digit widths change.
- Use `--crf 18` (default) for archival output. Use `--crf 23 --preset fast`
  for quick previews.
- If the source video has a metadata `creation_time` tag, you can extract it
  with `ffprobe -v quiet -print_format json -show_format input.mp4` and pass
  it directly as `--start-time`.
- For long or high-resolution videos, use a hardware encoder (`--codec h264_nvenc`,
  `--codec h264_vaapi`, etc.) with `--hwaccel` for a large speed improvement over
  the default `libx264`. Run `ffmpeg -encoders 2>/dev/null | grep h264` to see
  what your system supports.
- Use `--verbose` to watch a live progress bar during encoding.
