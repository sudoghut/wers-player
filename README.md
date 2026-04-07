# WERS Player

Small Python utility for playing the live WERS 88.9 stream outside the website player.

The script resolves the official WERS TuneGenie playlist, starts `ffplay`, and reconnects automatically if playback stops or the stream drops.

## Requirements

- Windows
- Python 3.11+ (tested with Python from Anaconda and Python 3.11)
- `ffplay` available in `PATH`

This repository was tested with:

- `python.exe` at `C:\Users\sudos\anaconda3\python.exe`
- `ffplay.exe` from FFmpeg at `C:\ProgramData\chocolatey\bin\ffplay.exe`

## Files

- `wers_player.py`: the reconnecting player
- `logs/wers-player.log`: runtime log file created by the script

## Usage

Run the player:

```powershell
python .\wers_player.py
```

Run with verbose logging:

```powershell
python .\wers_player.py --verbose
```

Use a custom `ffplay` path:

```powershell
python .\wers_player.py --ffplay-path "C:\path\to\ffplay.exe"
```

Pass extra arguments through to `ffplay`:

```powershell
python .\wers_player.py --ffplay-arg "-volume" --ffplay-arg "80"
```

## How It Works

The script:

1. Downloads the official WERS playlist from TuneGenie / StreamTheWorld.
2. Extracts the current stream candidate URLs from the `.pls` file.
3. Starts `ffplay` against the first candidate.
4. If playback exits, logs the failure and reconnects.
5. If a candidate fails quickly, tries the next candidate before backing off.

It uses the AAC playlist by default and falls back to the MP3 playlist if needed.

## Logging

By default the script writes logs to:

```text
logs/wers-player.log
```

The log includes:

- playlist resolution attempts
- selected stream candidates
- `ffplay` stderr output
- reconnect timing

## Notes

- This tool avoids the embedded browser player and connects to the stream source directly.
- Stream URLs are resolved fresh on reconnect, so CDN node changes are handled automatically.
