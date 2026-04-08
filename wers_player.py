import argparse
import configparser
import logging
import os
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
        reconnect_delay: float,
        max_reconnect_delay: float,
        healthy_run_seconds: float,
    ) -> None:
        self.resolver = resolver
        self.ffplay = ffplay
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.healthy_run_seconds = healthy_run_seconds
        self._stop = threading.Event()
        self._process: subprocess.Popen[str] | None = None

    def stop(self) -> None:
        self._stop.set()
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
                process, recent_lines = self.ffplay.start(stream_url)
                self._process = process
                return_code = process.wait()
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
    loop = PlayerLoop(
        resolver=resolver,
        ffplay=ffplay,
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
