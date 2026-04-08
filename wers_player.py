import argparse
import configparser
import http.client
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Iterable
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_AAC_PLAYLIST = "https://playerservices.streamtheworld.com/pls/WERSFMAAC.pls"
DEFAULT_MP3_PLAYLIST = "https://playerservices.streamtheworld.com/pls/WERSFM.pls"
DEFAULT_USER_AGENT = "wers-player/1.0"


class StreamResolver:
    def __init__(self, playlist_url: str, timeout: float, user_agent: str) -> None:
        self.playlist_url = playlist_url
        self.timeout = timeout
        self.user_agent = user_agent

    def resolve(self) -> list[str]:
        request = Request(self.playlist_url, headers={"User-Agent": self.user_agent})
        with urlopen(request, timeout=self.timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")

        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(payload)
        if "playlist" not in parser:
            raise ValueError(f"playlist file is missing [playlist]: {self.playlist_url}")

        section = parser["playlist"]
        entries: list[tuple[int, str]] = []
        for key, value in section.items():
            if not key.startswith("file"):
                continue
            suffix = key[4:]
            if suffix.isdigit() and value.strip():
                entries.append((int(suffix), value.strip()))

        entries.sort(key=lambda item: item[0])
        urls = [url for _, url in entries]
        if not urls:
            raise ValueError(f"no stream URLs found in {self.playlist_url}")
        return urls


def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children."""
    if sys.platform == "win32":
        # taskkill /T kills the entire process tree
        subprocess.call(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass



class IcyMetadataMonitor:
    """Monitors ICY metadata from a stream URL in a background thread."""

    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_title = ""

    def start(self, stream_url: str) -> None:
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._monitor, args=(stream_url,), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None

    def _monitor(self, stream_url: str) -> None:
        parsed = urlparse(stream_url)
        use_ssl = parsed.scheme == "https"
        host = parsed.hostname or ""
        port = parsed.port or (443 if use_ssl else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        try:
            if use_ssl:
                conn = http.client.HTTPSConnection(host, port, timeout=15)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=15)

            conn.request(
                "GET",
                path,
                headers={
                    "User-Agent": self.user_agent,
                    "Icy-MetaData": "1",
                },
            )
            resp = conn.getresponse()
            metaint_str = resp.getheader("icy-metaint")
            if not metaint_str:
                logging.debug("stream does not support ICY metadata")
                return
            metaint = int(metaint_str)
            logging.debug("ICY metaint: %d bytes", metaint)

            while not self._stop.is_set():
                # Skip audio data
                remaining = metaint
                while remaining > 0 and not self._stop.is_set():
                    chunk = resp.read(min(remaining, 4096))
                    if not chunk:
                        return
                    remaining -= len(chunk)

                # Read metadata length byte
                length_byte = resp.read(1)
                if not length_byte:
                    return
                meta_length = length_byte[0] * 16
                if meta_length == 0:
                    continue

                # Read metadata
                meta_data = resp.read(meta_length)
                if not meta_data:
                    return
                meta_str = meta_data.decode("utf-8", errors="replace").rstrip("\x00")
                match = re.search(r"StreamTitle='([^']*)'", meta_str)
                if match:
                    title = match.group(1).strip()
                    # Skip station identification (e.g. "WERS - Boston")
                    if title and title.upper().startswith("WERS"):
                        continue
                    if title and title != self._last_title:
                        self._last_title = title
                        logging.info("now playing: %s", title)
        except Exception:
            if not self._stop.is_set():
                logging.debug("ICY metadata monitor disconnected", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass


class FfplayProcess:
    def __init__(self, ffplay_path: str, extra_args: Iterable[str]) -> None:
        self.ffplay_path = ffplay_path
        self.extra_args = list(extra_args)

    def start(self, stream_url: str) -> tuple[subprocess.Popen[str], deque[str]]:
        recent_lines: deque[str] = deque(maxlen=50)
        command = [
            self.ffplay_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nodisp",
            "-vn",
            "-autoexit",
            *self.extra_args,
            stream_url,
        ]
        kwargs: dict = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)

        def drain_stderr() -> None:
            assert process.stderr is not None
            for raw_line in process.stderr:
                line = raw_line.rstrip()
                if not line:
                    continue
                recent_lines.append(line)
                logging.warning("ffplay: %s", line)

        thread = threading.Thread(target=drain_stderr, daemon=True)
        thread.start()
        return process, recent_lines


class PlayerLoop:
    def __init__(
        self,
        resolver: StreamResolver,
        ffplay: FfplayProcess,
        icy_monitor: IcyMetadataMonitor,
        reconnect_delay: float,
        max_reconnect_delay: float,
        healthy_run_seconds: float,
    ) -> None:
        self.resolver = resolver
        self.ffplay = ffplay
        self.icy_monitor = icy_monitor
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.healthy_run_seconds = healthy_run_seconds
        self._stop = threading.Event()
        self._process: subprocess.Popen[str] | None = None

    def stop(self) -> None:
        self._stop.set()
        self.icy_monitor.stop()
        if self._process and self._process.poll() is None:
            logging.info("stopping ffplay")
            _kill_process_tree(self._process.pid)

    def run(self) -> int:
        delay = self.reconnect_delay
        cycle = 0
        while not self._stop.is_set():
            cycle += 1
            try:
                candidates = self.resolver.resolve()
            except Exception as exc:
                logging.exception("failed to resolve stream playlist: %s", exc)
                self._sleep_with_stop(delay)
                delay = min(delay * 2, self.max_reconnect_delay)
                continue

            logging.info("cycle %s: resolved %s stream candidate(s)", cycle, len(candidates))
            played_long_enough = False

            for index, stream_url in enumerate(candidates, start=1):
                if self._stop.is_set():
                    break
                logging.info("starting candidate %s/%s: %s", index, len(candidates), stream_url)
                start_time = time.monotonic()
                self.icy_monitor.start(stream_url)
                process, recent_lines = self.ffplay.start(stream_url)
                self._process = process
                return_code = process.wait()
                self.icy_monitor.stop()
                # Ensure the entire process tree is cleaned up (the shim
                # may have exited while the real ffplay child is still alive).
                _kill_process_tree(process.pid)
                self._process = None
                runtime = time.monotonic() - start_time

                if self._stop.is_set():
                    return 0

                if return_code == 0:
                    logging.info("ffplay exited cleanly after %.1f seconds", runtime)
                else:
                    logging.warning(
                        "ffplay exited with code %s after %.1f seconds", return_code, runtime
                    )
                    if recent_lines:
                        logging.warning("last ffplay lines: %s", " | ".join(recent_lines))

                if runtime >= self.healthy_run_seconds:
                    played_long_enough = True
                    delay = self.reconnect_delay
                    logging.info(
                        "stream had been healthy for %.1f seconds; reconnecting immediately",
                        runtime,
                    )
                    break

                logging.info(
                    "candidate %s ended too quickly; trying next candidate if available",
                    index,
                )

            if self._stop.is_set():
                break

            if not played_long_enough:
                logging.info("all candidates ended quickly in cycle %s", cycle)
                self._sleep_with_stop(delay)
                delay = min(delay * 2, self.max_reconnect_delay)

        return 0

    def _sleep_with_stop(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))


def choose_ffplay(explicit_path: str | None) -> str:
    if explicit_path:
        return explicit_path
    discovered = shutil.which("ffplay")
    if not discovered:
        raise FileNotFoundError(
            "ffplay was not found in PATH. Install FFmpeg or pass --ffplay-path."
        )
    return discovered


def configure_logging(log_file: Path, verbose: bool) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Play the WERS online stream with automatic reconnects."
    )
    parser.add_argument(
        "--playlist-url",
        default=DEFAULT_AAC_PLAYLIST,
        help=f"Playlist URL to resolve before each reconnect. Default: {DEFAULT_AAC_PLAYLIST}",
    )
    parser.add_argument(
        "--fallback-playlist-url",
        default=DEFAULT_MP3_PLAYLIST,
        help="Fallback playlist used when the primary playlist cannot be resolved.",
    )
    parser.add_argument(
        "--ffplay-path",
        help="Path to ffplay.exe. If omitted, the script uses the one found in PATH.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=15.0,
        help="Network timeout in seconds for playlist downloads.",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=3.0,
        help="Initial reconnect delay in seconds after repeated quick failures.",
    )
    parser.add_argument(
        "--max-reconnect-delay",
        type=float,
        default=30.0,
        help="Maximum reconnect delay in seconds.",
    )
    parser.add_argument(
        "--healthy-run-seconds",
        type=float,
        default=30.0,
        help="If playback lasts at least this long, reconnect immediately on the next drop.",
    )
    parser.add_argument(
        "--log-file",
        default="logs/wers-player.log",
        help="Path to the log file.",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="HTTP User-Agent used when fetching playlists.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable more verbose logging.",
    )
    parser.add_argument(
        "--ffplay-arg",
        action="append",
        default=[],
        help="Additional argument passed through to ffplay. Can be used multiple times.",
    )
    return parser


class FallbackResolver(StreamResolver):
    def __init__(
        self,
        playlist_url: str,
        fallback_playlist_url: str | None,
        timeout: float,
        user_agent: str,
    ) -> None:
        super().__init__(playlist_url, timeout, user_agent)
        self.fallback_playlist_url = fallback_playlist_url

    def resolve(self) -> list[str]:
        errors: list[str] = []
        for url in [self.playlist_url, self.fallback_playlist_url]:
            if not url:
                continue
            self.playlist_url = url
            try:
                urls = super().resolve()
                logging.info("resolved playlist from %s", url)
                return urls
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                logging.warning("failed to resolve %s: %s", url, exc)
        raise RuntimeError("; ".join(errors))


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(Path(args.log_file), args.verbose)

    try:
        ffplay_path = choose_ffplay(args.ffplay_path)
    except Exception as exc:
        logging.error("%s", exc)
        return 2

    logging.info("using ffplay at %s", ffplay_path)
    resolver = FallbackResolver(
        playlist_url=args.playlist_url,
        fallback_playlist_url=args.fallback_playlist_url,
        timeout=args.request_timeout,
        user_agent=args.user_agent,
    )
    ffplay = FfplayProcess(ffplay_path=ffplay_path, extra_args=args.ffplay_arg)
    icy_monitor = IcyMetadataMonitor(user_agent=args.user_agent)
    loop = PlayerLoop(
        resolver=resolver,
        ffplay=ffplay,
        icy_monitor=icy_monitor,
        reconnect_delay=args.reconnect_delay,
        max_reconnect_delay=args.max_reconnect_delay,
        healthy_run_seconds=args.healthy_run_seconds,
    )

    def handle_signal(_signum: int, _frame: object) -> None:
        logging.info("shutdown signal received")
        loop.stop()

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    try:
        return loop.run()
    except KeyboardInterrupt:
        logging.info("keyboard interrupt received")
        loop.stop()
        return 0
    except URLError as exc:
        logging.exception("network error: %s", exc)
        return 1
    except Exception as exc:
        logging.exception("unexpected error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
