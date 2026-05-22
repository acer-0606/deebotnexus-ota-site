import hashlib
import importlib.util
import json
import pathlib
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "tools" / "local_ota_mirror.py"


def load_module():
    spec = importlib.util.spec_from_file_location("local_ota_mirror", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalOtaMirrorTests(unittest.TestCase):
    def test_config_file_supplies_interval_and_github_proxy(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            config_path = root / "mirror-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "port": 19090,
                        "interval": 42,
                        "github_proxy": "http://127.0.0.1:7890",
                        "github_proxy_username": "dev@example.com",
                        "github_proxy_password": "pa:ss@word",
                        "advertise_host": "192.168.1.20",
                    },
                ),
                encoding="utf-8",
            )

            parser = mirror.build_parser()
            args = parser.parse_args(["run", "--config", str(config_path)])
            mirror.apply_config(args)

            self.assertEqual(args.port, 19090)
            self.assertEqual(args.interval, 42)
            self.assertEqual(args.github_proxy, "http://127.0.0.1:7890")
            self.assertEqual(args.github_proxy_username, "dev@example.com")
            self.assertEqual(args.github_proxy_password, "pa:ss@word")
            self.assertEqual(
                mirror.resolve_github_proxy(args),
                "http://dev%40example.com:pa%3Ass%40word@127.0.0.1:7890",
            )
            self.assertEqual(
                mirror.public_base_url(args),
                "http://192.168.1.20:19090",
            )

            args = parser.parse_args(["run", "--config", str(config_path), "--interval", "7"])
            mirror.apply_config(args)

            self.assertEqual(args.interval, 7)

    def test_github_proxy_applies_only_to_github_hosts(self):
        mirror = load_module()
        proxy = "http://127.0.0.1:7890"

        self.assertEqual(mirror.github_proxy_for_url("https://github.com/a/b", proxy), proxy)
        self.assertEqual(mirror.github_proxy_for_url("https://acer-0606.github.io/site", proxy), proxy)
        self.assertEqual(
            mirror.github_proxy_for_url("https://objects.githubusercontent.com/a/b", proxy),
            proxy,
        )
        self.assertEqual(mirror.github_proxy_for_url("http://192.168.1.20:18080/a", proxy), "")
        self.assertEqual(mirror.github_proxy_for_url("https://example.com/a", proxy), "")
        self.assertEqual(mirror.github_proxy_for_url("file:///tmp/a.dn-ota", proxy), "")

    def test_sync_downloads_packages_and_rewrites_metadata_urls(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            asset = remote / "DeebotNexus-main-macos-1.2.3.dn-ota"
            asset.write_bytes(b"offline ota package")
            asset_sha = hashlib.sha256(asset.read_bytes()).hexdigest()
            asset_url = asset.resolve().as_uri()

            latest = {
                "version": "1.2.3",
                "notes": "test release",
                "pub_date": "2026-05-22T00:00:00Z",
                "platforms": {
                    "darwin-aarch64": {
                        "url": asset_url,
                        "sha256": asset_sha,
                        "signature": "test",
                    },
                },
            }
            manifest = {
                "schemaVersion": 1,
                "channel": "stable",
                "publishedAt": "2026-05-22T00:00:00Z",
                "minimumHostVersion": "0.1.0",
                "app": {
                    "version": "1.2.3",
                    "changelog": "test release",
                    "platforms": {
                        "darwin-aarch64": {
                            "url": asset_url,
                            "sha256": asset_sha,
                            "tauriSignature": "test",
                        },
                    },
                },
                "plugins": [],
                "signature": "manifest-signature",
            }
            connect = {
                "schemaVersion": 1,
                "bundleId": "connect-tools-configs",
                "version": "2026.05.22.1",
                "url": asset_url,
                "sha256": asset_sha,
            }
            (remote / "latest.json").write_text(json.dumps(latest), encoding="utf-8")
            (remote / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (remote / "connect-tools-configs.json").write_text(json.dumps(connect), encoding="utf-8")

            cache_dir = root / "cache"
            result = mirror.sync_mirror(
                remote_base_url=remote.resolve().as_uri(),
                cache_dir=cache_dir,
                public_base_url="http://192.168.1.20:18080",
                timeout=2,
            )

            self.assertEqual(result.download_count, 1)
            snapshot_dir = cache_dir / "snapshots" / result.snapshot_name
            current = json.loads((cache_dir / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(current["snapshot"], result.snapshot_name)
            mirrored_asset = snapshot_dir / "assets" / asset.name
            self.assertEqual(mirrored_asset.read_bytes(), b"offline ota package")

            local_latest = json.loads((snapshot_dir / "latest.json").read_text(encoding="utf-8"))
            local_manifest = json.loads(
                (snapshot_dir / "manifest.json").read_text(encoding="utf-8"),
            )
            local_connect = json.loads(
                (snapshot_dir / "connect-tools-configs.json").read_text(encoding="utf-8"),
            )
            expected_url = f"http://192.168.1.20:18080/snapshots/{result.snapshot_name}/assets/{asset.name}"

            self.assertEqual(local_latest["platforms"]["darwin-aarch64"]["url"], expected_url)
            self.assertEqual(
                local_manifest["app"]["platforms"]["darwin-aarch64"]["url"],
                expected_url,
            )
            self.assertEqual(local_connect["url"], expected_url)

    def test_sync_rejects_sha256_mismatch(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            asset = remote / "bad.dn-ota"
            asset.write_bytes(b"tampered")
            latest = {
                "version": "1.0.0",
                "platforms": {
                    "darwin-aarch64": {
                        "url": asset.resolve().as_uri(),
                        "sha256": "0" * 64,
                    },
                },
            }
            (remote / "latest.json").write_text(json.dumps(latest), encoding="utf-8")
            (remote / "manifest.json").write_text(
                json.dumps({"schemaVersion": 1, "app": {"platforms": {}}, "plugins": []}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
                mirror.sync_mirror(
                    remote_base_url=remote.resolve().as_uri(),
                    cache_dir=root / "cache",
                    public_base_url="http://192.168.1.20:18080",
                    timeout=2,
                )

    def test_failed_sync_does_not_switch_current_snapshot(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            good_remote = root / "good-remote"
            good_remote.mkdir()
            good_asset = good_remote / "good.dn-ota"
            good_asset.write_bytes(b"good ota package")
            good_sha = hashlib.sha256(good_asset.read_bytes()).hexdigest()
            good_latest = {
                "version": "1.0.0",
                "platforms": {
                    "darwin-aarch64": {
                        "url": good_asset.resolve().as_uri(),
                        "sha256": good_sha,
                    },
                },
            }
            (good_remote / "latest.json").write_text(json.dumps(good_latest), encoding="utf-8")
            (good_remote / "manifest.json").write_text(
                json.dumps({"schemaVersion": 1, "app": {"platforms": {}}, "plugins": []}),
                encoding="utf-8",
            )

            cache_dir = root / "cache"
            first = mirror.sync_mirror(
                remote_base_url=good_remote.resolve().as_uri(),
                cache_dir=cache_dir,
                public_base_url="http://192.168.1.20:18080",
                timeout=2,
            )

            bad_remote = root / "bad-remote"
            bad_remote.mkdir()
            bad_asset = bad_remote / "bad.dn-ota"
            bad_asset.write_bytes(b"bad ota package")
            bad_latest = {
                "version": "2.0.0",
                "platforms": {
                    "darwin-aarch64": {
                        "url": bad_asset.resolve().as_uri(),
                        "sha256": "0" * 64,
                    },
                },
            }
            (bad_remote / "latest.json").write_text(json.dumps(bad_latest), encoding="utf-8")
            (bad_remote / "manifest.json").write_text(
                json.dumps({"schemaVersion": 1, "app": {"platforms": {}}, "plugins": []}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
                mirror.sync_mirror(
                    remote_base_url=bad_remote.resolve().as_uri(),
                    cache_dir=cache_dir,
                    public_base_url="http://192.168.1.20:18080",
                    timeout=2,
                )

            current = json.loads((cache_dir / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(current["snapshot"], first.snapshot_name)
            self.assertTrue((cache_dir / "snapshots" / first.snapshot_name / "assets" / "good.dn-ota").exists())

    def test_serve_mirror_exposes_metadata_and_assets(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            asset = remote / "plugin-1.0.0.dn-ota"
            asset.write_bytes(b"plugin ota package")
            asset_sha = hashlib.sha256(asset.read_bytes()).hexdigest()
            asset_url = asset.resolve().as_uri()
            latest = {
                "version": "1.0.0",
                "platforms": {
                    "darwin-aarch64": {
                        "url": asset_url,
                        "sha256": asset_sha,
                    },
                },
            }
            manifest = {
                "schemaVersion": 1,
                "app": {"version": "1.0.0", "platforms": {}},
                "plugins": [
                    {
                        "id": "plugin",
                        "version": "1.0.0",
                        "url": asset_url,
                        "sha256": asset_sha,
                    },
                ],
            }
            (remote / "latest.json").write_text(json.dumps(latest), encoding="utf-8")
            (remote / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            cache_dir = root / "cache"
            result = mirror.sync_mirror(
                remote_base_url=remote.resolve().as_uri(),
                cache_dir=cache_dir,
                public_base_url="http://127.0.0.1:18080",
                timeout=2,
            )

            server = mirror.serve_mirror(cache_dir, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(f"{base_url}/manifest.json", timeout=2) as response:
                    served_manifest = json.loads(response.read().decode("utf-8"))
                asset_path = urllib.parse.urlparse(served_manifest["plugins"][0]["url"]).path
                with urllib.request.urlopen(
                    f"{base_url}{asset_path}",
                    timeout=2,
                ) as response:
                    served_asset = response.read()

                self.assertEqual(
                    served_manifest["plugins"][0]["url"],
                    f"http://127.0.0.1:18080/snapshots/{result.snapshot_name}/assets/plugin-1.0.0.dn-ota",
                )
                self.assertEqual(served_asset, b"plugin ota package")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
