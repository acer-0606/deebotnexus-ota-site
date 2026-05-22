#!/usr/bin/env python3
"""Local LAN mirror for DeebotNexus OTA metadata and .dn-ota packages."""

import argparse
import copy
import hashlib
import http.server
import json
import os
import posixpath
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


SITE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = SITE_ROOT / ".local-ota"
DEFAULT_CONFIG_PATH = DEFAULT_CACHE_DIR / "config.json"
DEFAULT_REMOTE_BASE_URL = "https://acer-0606.github.io/deebotnexus-ota-site"
METADATA_FILES = ("latest.json", "manifest.json", "connect-tools-configs.json")
REQUIRED_METADATA_FILES = ("latest.json", "manifest.json")
USER_AGENT = "DeebotNexusLocalOtaMirror/1.0"
GITHUB_PROXY_DOMAINS = ("github.com", "github.io", "githubusercontent.com")


class DownloadRef:
    def __init__(self, url, sha256, asset_name):
        self.url = url
        self.sha256 = sha256
        self.asset_name = asset_name


class SyncResult:
    def __init__(self, metadata_count, asset_count, download_count, reused_count, snapshot_name, changed):
        self.metadata_count = metadata_count
        self.asset_count = asset_count
        self.download_count = download_count
        self.reused_count = reused_count
        self.snapshot_name = snapshot_name
        self.changed = changed


def sync_mirror(remote_base_url, cache_dir, public_base_url, timeout=30, github_proxy=""):
    """Fetch remote metadata/assets and write a self-contained local mirror."""
    cache_dir = Path(cache_dir)
    snapshots_dir = cache_dir / "snapshots"
    metadata = fetch_remote_metadata(remote_base_url, timeout, github_proxy=github_proxy)
    refs_by_url = collect_download_refs(metadata)
    unique_refs = unique_refs_by_asset(refs_by_url)
    public_base_url = public_base_url.rstrip("/")
    metadata_digest = digest_metadata(metadata)

    reusable_snapshot = reusable_current_snapshot(
        cache_dir,
        remote_base_url=remote_base_url,
        public_base_url=public_base_url,
        metadata_digest=metadata_digest,
        refs=unique_refs,
    )
    if reusable_snapshot:
        return SyncResult(
            metadata_count=len(metadata),
            asset_count=len(unique_refs),
            download_count=0,
            reused_count=len(unique_refs),
            snapshot_name=reusable_snapshot,
            changed=False,
        )

    snapshot_name = new_snapshot_name()
    snapshot_dir = snapshots_dir / snapshot_name
    staging_dir = snapshots_dir / (snapshot_name + ".tmp")
    assets_dir = staging_dir / "assets"
    download_count = 0
    reused_count = 0

    if staging_dir.exists():
        shutil.rmtree(staging_dir)

    try:
        assets_dir.mkdir(parents=True, exist_ok=True)
        for ref in unique_refs:
            if copy_existing_asset(cache_dir, ref, assets_dir / ref.asset_name):
                reused_count += 1
            elif ensure_asset(ref, assets_dir, timeout, github_proxy=github_proxy):
                download_count += 1
            else:
                reused_count += 1

        url_to_asset = {url: ref.asset_name for url, ref in refs_by_url.items()}
        snapshot_public_base_url = "%s/snapshots/%s" % (
            public_base_url,
            urllib.parse.quote(snapshot_name),
        )
        for name, document in metadata.items():
            rewritten = rewrite_metadata_urls(document, url_to_asset, snapshot_public_base_url)
            write_json_atomic(staging_dir / name, rewritten)

        os.replace(str(staging_dir), str(snapshot_dir))
        write_json_atomic(
            cache_dir / "current.json",
            {
                "snapshot": snapshot_name,
                "updatedAt": current_timestamp(),
            },
        )
        write_json_atomic(
            cache_dir / "state.json",
            {
                "remoteBaseUrl": remote_base_url.rstrip("/"),
                "publicBaseUrl": public_base_url,
                "metadataDigest": metadata_digest,
                "metadataFiles": sorted(metadata.keys()),
                "assetCount": len(unique_refs),
                "snapshot": snapshot_name,
                "lastSyncedAt": current_timestamp(),
            },
        )
        return SyncResult(
            metadata_count=len(metadata),
            asset_count=len(unique_refs),
            download_count=download_count,
            reused_count=reused_count,
            snapshot_name=snapshot_name,
            changed=True,
        )
    except Exception:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        raise


def fetch_remote_metadata(remote_base_url, timeout, github_proxy=""):
    remote_base_url = remote_base_url.rstrip("/") + "/"
    metadata = {}
    for name in METADATA_FILES:
        url = urllib.parse.urljoin(remote_base_url, name)
        try:
            metadata[name] = json.loads(fetch_bytes(url, timeout, github_proxy=github_proxy).decode("utf-8"))
        except Exception as error:
            if name in REQUIRED_METADATA_FILES:
                raise RuntimeError("failed to fetch required metadata %s: %s" % (url, error))
            if not is_missing_optional_file(error):
                raise RuntimeError("failed to fetch optional metadata %s: %s" % (url, error))
    return metadata


def is_missing_optional_file(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 404
    if isinstance(error, urllib.error.URLError):
        return isinstance(error.reason, FileNotFoundError)
    return isinstance(error, FileNotFoundError)


def collect_download_refs(metadata):
    refs_by_url = {}
    refs_by_name = {}
    for document_name, document in metadata.items():
        for url, sha256 in iter_ota_urls(document):
            if not sha256 or not is_sha256(sha256):
                raise ValueError("%s references %s without a valid sha256" % (document_name, url))
            asset_name = asset_name_from_url(url)
            ref = DownloadRef(url=url, sha256=sha256.lower(), asset_name=asset_name)
            existing = refs_by_url.get(url)
            if existing:
                if existing.sha256 != ref.sha256:
                    raise ValueError("conflicting sha256 for %s" % url)
                continue
            same_name = refs_by_name.get(asset_name)
            if same_name and same_name.sha256 != ref.sha256:
                raise ValueError(
                    "asset name collision for %s with different sha256 values" % asset_name,
                )
            refs_by_url[url] = ref
            refs_by_name.setdefault(asset_name, ref)
    return refs_by_url


def iter_ota_urls(value):
    if isinstance(value, dict):
        url = value.get("url")
        if isinstance(url, str) and ota_url(url):
            yield url, value.get("sha256")
        for child in value.values():
            yield from iter_ota_urls(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_ota_urls(child)


def ota_url(url):
    parsed = urllib.parse.urlparse(url)
    return parsed.path.endswith(".dn-ota")


def asset_name_from_url(url):
    parsed = urllib.parse.urlparse(url)
    name = Path(urllib.parse.unquote(parsed.path)).name
    if not name or name in (".", "..") or "/" in name:
        raise ValueError("invalid OTA asset URL: %s" % url)
    if not name.endswith(".dn-ota"):
        raise ValueError("OTA asset does not end with .dn-ota: %s" % url)
    return name


def is_sha256(value):
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def unique_refs_by_asset(refs_by_url):
    refs_by_name = {}
    for ref in refs_by_url.values():
        refs_by_name.setdefault(ref.asset_name, ref)
    return list(refs_by_name.values())


def digest_metadata(metadata):
    encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def reusable_current_snapshot(cache_dir, remote_base_url, public_base_url, metadata_digest, refs):
    state_path = Path(cache_dir) / "state.json"
    if not state_path.exists():
        return ""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        snapshot_name = current_snapshot_name(cache_dir)
    except Exception:
        return ""
    if state.get("remoteBaseUrl") != remote_base_url.rstrip("/"):
        return ""
    if state.get("publicBaseUrl") != public_base_url.rstrip("/"):
        return ""
    if state.get("metadataDigest") != metadata_digest:
        return ""
    snapshot_dir = snapshot_path(cache_dir, snapshot_name)
    if not snapshot_dir.is_dir():
        return ""
    for ref in refs:
        asset_path = snapshot_dir / "assets" / ref.asset_name
        if not asset_path.exists() or sha256_file(asset_path) != ref.sha256:
            return ""
    return snapshot_name


def copy_existing_asset(cache_dir, ref, target):
    snapshot_name = ""
    try:
        snapshot_name = current_snapshot_name(cache_dir)
    except Exception:
        return False
    source = snapshot_path(cache_dir, snapshot_name) / "assets" / ref.asset_name
    if not source.exists() or sha256_file(source) != ref.sha256:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def new_snapshot_name():
    return "%s-%s" % (
        time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        uuid.uuid4().hex[:8],
    )


def ensure_asset(ref, assets_dir, timeout, github_proxy=""):
    target = assets_dir / ref.asset_name
    if target.exists() and sha256_file(target) == ref.sha256:
        return False

    tmp = target.with_name(target.name + ".tmp")
    try:
        digest = download_to_file(ref.url, tmp, timeout, github_proxy=github_proxy)
        if digest != ref.sha256:
            raise ValueError(
                "sha256 mismatch for %s: expected %s, got %s"
                % (ref.url, ref.sha256, digest),
            )
        os.replace(str(tmp), str(target))
        return True
    finally:
        if tmp.exists():
            tmp.unlink()


def download_to_file(url, target, timeout, github_proxy=""):
    hasher = hashlib.sha256()
    with open_url(url, timeout, github_proxy=github_proxy) as response:
        with target.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                output.write(chunk)
    return hasher.hexdigest()


def sha256_file(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def rewrite_metadata_urls(value, url_to_asset, public_base_url):
    copied = copy.deepcopy(value)
    return rewrite_metadata_value(copied, url_to_asset, public_base_url.rstrip("/"))


def rewrite_metadata_value(value, url_to_asset, public_base_url):
    if isinstance(value, dict):
        for key, child in list(value.items()):
            if key == "url" and isinstance(child, str) and child in url_to_asset:
                value[key] = "%s/assets/%s" % (
                    public_base_url,
                    urllib.parse.quote(url_to_asset[child]),
                )
            else:
                value[key] = rewrite_metadata_value(child, url_to_asset, public_base_url)
        return value
    if isinstance(value, list):
        return [rewrite_metadata_value(child, url_to_asset, public_base_url) for child in value]
    return value


def fetch_bytes(url, timeout, github_proxy=""):
    with open_url(url, timeout, github_proxy=github_proxy) as response:
        return response.read()


def open_url(url, timeout, github_proxy=""):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    proxy_url = github_proxy_for_url(url, github_proxy)
    if proxy_url:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}),
        )
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def github_proxy_for_url(url, github_proxy):
    if not github_proxy:
        return ""
    host = urllib.parse.urlparse(url).hostname
    if not host:
        return ""
    host = host.lower()
    for domain in GITHUB_PROXY_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return github_proxy
    return ""


def resolve_github_proxy(args):
    proxy = normalize_proxy_url(getattr(args, "github_proxy", "") or "")
    if not proxy:
        return ""
    username = getattr(args, "github_proxy_username", "") or ""
    password = getattr(args, "github_proxy_password", "") or ""
    if not username and not password:
        return proxy

    parsed = urllib.parse.urlsplit(proxy)
    if "@" in parsed.netloc:
        return proxy

    auth = "%s:%s@" % (
        urllib.parse.quote(username, safe=""),
        urllib.parse.quote(password, safe=""),
    )
    return urllib.parse.urlunsplit(
        (parsed.scheme, auth + parsed.netloc, parsed.path, parsed.query, parsed.fragment),
    )


def normalize_proxy_url(proxy):
    proxy = proxy.strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        return "http://" + proxy
    return proxy


def write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def current_timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def resolve_cache_dir(value):
    if value:
        return Path(value).expanduser().resolve()
    return DEFAULT_CACHE_DIR


def apply_config(args):
    config = read_config(args.config)
    apply_string_config(args, config, "remote_base_url", DEFAULT_REMOTE_BASE_URL)
    apply_string_config(args, config, "cache_dir", "")
    apply_string_config(args, config, "base_url", "")
    apply_string_config(args, config, "advertise_host", "")
    apply_string_config(args, config, "github_proxy", "")
    apply_string_config(args, config, "github_proxy_username", "")
    apply_string_config(args, config, "github_proxy_password", "")
    apply_int_config(args, config, "port", 18080)
    apply_int_config(args, config, "timeout", 30)
    if hasattr(args, "bind"):
        apply_string_config(args, config, "bind", "0.0.0.0")
    if hasattr(args, "interval"):
        apply_int_config(args, config, "interval", 300)


def read_config(config_path):
    required = bool(config_path)
    path = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        if required:
            raise RuntimeError("config file does not exist: %s" % path)
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("config file must contain a JSON object: %s" % path)
    return value


def apply_string_config(args, config, name, default):
    current = getattr(args, name, None)
    if current is not None:
        setattr(args, name, current)
        return
    value = config.get(name, default)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise RuntimeError("config field %s must be a string" % name)
    setattr(args, name, value)


def apply_int_config(args, config, name, default):
    current = getattr(args, name, None)
    if current is not None:
        setattr(args, name, current)
        return
    value = config.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("config field %s must be an integer" % name)
    if value <= 0:
        raise RuntimeError("config field %s must be greater than zero" % name)
    setattr(args, name, value)


def public_base_url(args):
    if args.base_url:
        return args.base_url.rstrip("/")
    host = args.advertise_host or detect_lan_ip()
    return "http://%s:%s" % (host, args.port)


def detect_lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def require_current_snapshot(cache_dir):
    snapshot_dir = snapshot_path(cache_dir, current_snapshot_name(cache_dir))
    missing = [name for name in REQUIRED_METADATA_FILES if not (snapshot_dir / name).exists()]
    if missing:
        raise RuntimeError(
            "local mirror snapshot is incomplete; missing %s. Run the sync command first."
            % ", ".join(missing),
        )
    return snapshot_dir


def current_snapshot_name(cache_dir):
    pointer_path = Path(cache_dir) / "current.json"
    if not pointer_path.exists():
        raise RuntimeError("local mirror is not synced yet; missing current.json. Run the sync command first.")
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    snapshot_name = pointer.get("snapshot")
    if not valid_snapshot_name(snapshot_name):
        raise RuntimeError("invalid current snapshot pointer in %s" % pointer_path)
    return snapshot_name


def valid_snapshot_name(value):
    if not isinstance(value, str) or not value:
        return False
    return "/" not in value and "\\" not in value and value not in (".", "..")


def snapshot_path(cache_dir, snapshot_name):
    return Path(cache_dir) / "snapshots" / snapshot_name


class OtaMirrorRequestHandler(http.server.SimpleHTTPRequestHandler):
    cache_dir = None

    def translate_path(self, path):
        path = path.split("?", 1)[0]
        path = path.split("#", 1)[0]
        path = posixpath.normpath(urllib.parse.unquote(path))
        parts = [part for part in path.split("/") if part and part not in (".", "..")]
        if parts and parts[0] == "snapshots":
            resolved = Path(self.cache_dir)
        else:
            resolved = require_current_snapshot(self.cache_dir)
        for part in parts:
            resolved = resolved / part
        return str(resolved)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        if self.path.endswith(".json"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, format, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), format % args))


def make_handler(cache_dir):
    class Handler(OtaMirrorRequestHandler):
        pass

    Handler.cache_dir = Path(cache_dir)

    return Handler


def serve_mirror(cache_dir, bind, port):
    require_current_snapshot(cache_dir)
    server = http.server.ThreadingHTTPServer((bind, port), make_handler(cache_dir))
    return server


def run_polling_mirror(args):
    cache_dir = resolve_cache_dir(args.cache_dir)
    base_url = public_base_url(args)
    github_proxy = resolve_github_proxy(args)
    sync_lock = threading.Lock()
    stop_event = threading.Event()

    def sync_once(label):
        with sync_lock:
            result = sync_mirror(
                args.remote_base_url,
                cache_dir,
                base_url,
                timeout=args.timeout,
                github_proxy=github_proxy,
            )
        print(
            "%s: synced %s metadata files, %s assets (%s downloaded, %s reused)"
            % (label, result.metadata_count, result.asset_count, result.download_count, result.reused_count),
            flush=True,
        )

    try:
        sync_once("initial")
    except Exception as error:
        try:
            require_current_snapshot(cache_dir)
        except Exception:
            raise
        print("initial sync failed, serving existing mirror: %s" % error, file=sys.stderr)

    server = serve_mirror(cache_dir, args.bind, args.port)

    def poll_loop():
        while not stop_event.wait(args.interval):
            try:
                sync_once("poll")
            except Exception as error:
                print("poll sync failed: %s" % error, file=sys.stderr, flush=True)

    thread = threading.Thread(target=poll_loop, name="ota-mirror-poller", daemon=True)
    thread.start()
    print_server_urls(base_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()


def print_server_urls(base_url):
    base_url = base_url.rstrip("/")
    print("Local OTA mirror serving:", flush=True)
    print("  latest:   %s/latest.json" % base_url, flush=True)
    print("  manifest: %s/manifest.json" % base_url, flush=True)
    print("  assets:   %s/assets/<name>.dn-ota" % base_url, flush=True)


def command_sync(args):
    github_proxy = resolve_github_proxy(args)
    result = sync_mirror(
        remote_base_url=args.remote_base_url,
        cache_dir=resolve_cache_dir(args.cache_dir),
        public_base_url=public_base_url(args),
        timeout=args.timeout,
        github_proxy=github_proxy,
    )
    print(
        "synced %s metadata files, %s assets (%s downloaded, %s reused)"
        % (result.metadata_count, result.asset_count, result.download_count, result.reused_count),
    )


def command_serve(args):
    base_url = public_base_url(args)
    server = serve_mirror(resolve_cache_dir(args.cache_dir), args.bind, args.port)
    print_server_urls(base_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    finally:
        server.server_close()


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def add_common_sync_args(parser):
    parser.add_argument(
        "--config",
        default=None,
        help="JSON config file (default: subrepos/ota-site/.local-ota/config.json if it exists)",
    )
    parser.add_argument(
        "--remote-base-url",
        default=None,
        help="remote OTA site base URL (default: https://acer-0606.github.io/deebotnexus-ota-site)",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="local mirror cache directory (default: subrepos/ota-site/.local-ota)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="public URL written into mirrored metadata; overrides --advertise-host",
    )
    parser.add_argument(
        "--advertise-host",
        default=None,
        help="LAN host/IP written into mirrored metadata when --base-url is not set",
    )
    parser.add_argument(
        "--github-proxy",
        default=None,
        help="proxy URL used only for GitHub metadata and asset requests",
    )
    parser.add_argument(
        "--github-proxy-username",
        default=None,
        help="optional GitHub proxy username; prefer config files for real credentials",
    )
    parser.add_argument(
        "--github-proxy-password",
        default=None,
        help="optional GitHub proxy password; prefer config files for real credentials",
    )
    parser.add_argument("--port", type=positive_int, default=None, help="LAN HTTP port")
    parser.add_argument("--timeout", type=positive_int, default=None, help="network timeout in seconds")


def add_common_serve_args(parser):
    parser.add_argument(
        "--config",
        default=None,
        help="JSON config file (default: subrepos/ota-site/.local-ota/config.json if it exists)",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="local mirror cache directory (default: subrepos/ota-site/.local-ota)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="URL to display for clients; overrides --advertise-host",
    )
    parser.add_argument(
        "--advertise-host",
        default=None,
        help="LAN host/IP to display when --base-url is not set",
    )
    parser.add_argument("--bind", default=None, help="address to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=positive_int, default=None, help="LAN HTTP port")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="sync metadata and OTA packages once")
    add_common_sync_args(sync_parser)
    sync_parser.set_defaults(func=command_sync)

    serve_parser = subparsers.add_parser("serve", help="serve an existing local mirror")
    add_common_serve_args(serve_parser)
    serve_parser.set_defaults(func=command_serve)

    run_parser = subparsers.add_parser("run", help="sync once, serve, then poll for updates")
    add_common_sync_args(run_parser)
    run_parser.add_argument("--bind", default=None, help="address to bind (default: 0.0.0.0)")
    run_parser.add_argument(
        "--interval",
        type=positive_int,
        default=None,
        help="poll interval in seconds (default: 300)",
    )
    run_parser.set_defaults(func=run_polling_mirror)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        apply_config(args)
        args.func(args)
    except Exception as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
