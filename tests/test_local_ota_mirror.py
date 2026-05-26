import hashlib
import importlib.util
import json
import os
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "tools" / "local_ota_mirror.py"


def load_module():
    spec = importlib.util.spec_from_file_location("local_ota_mirror", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_snapshot_remote(remote, snapshot_id="20260523T000000Z-v2test", target_bytes=b"snapshot ota package"):
    snapshot_remote_dir = remote / "snapshots" / snapshot_id
    snapshot_remote_dir.mkdir(parents=True, exist_ok=True)

    asset_sha = hashlib.sha256(target_bytes).hexdigest()
    asset_name = f"sha256-{asset_sha[:12]}-DeebotNexus-main-darwin-aarch64-2.0.0.dn-ota"
    asset = remote / asset_name
    asset.write_bytes(target_bytes)
    asset_url = asset.resolve().as_uri()

    snapshot = {
        "schemaVersion": 1,
        "kind": "deebotOtaSnapshot",
        "snapshotId": snapshot_id,
        "channel": "stable",
        "version": 2,
        "publishedAt": "2026-05-23T00:00:00Z",
        "minimumHostVersion": "0.1.0",
        "metadata": [],
        "targets": [
            {
                "targetId": "app:darwin-aarch64:2.0.0",
                "kind": "app",
                "platform": "darwin-aarch64",
                "version": "2.0.0",
                "assetName": asset_name,
                "sha256": asset_sha,
                "length": len(target_bytes),
                "locations": [
                    {"kind": "githubRelease", "url": asset_url},
                    {"kind": "snapshotMirror", "path": f"assets/{asset_name}"},
                ],
            },
        ],
        "signature": "snapshot-signature",
    }
    snapshot_text = json.dumps(snapshot, sort_keys=True)
    snapshot_bytes = snapshot_text.encode("utf-8")
    (snapshot_remote_dir / "snapshot.json").write_text(snapshot_text, encoding="utf-8")

    timestamp = {
        "schemaVersion": 1,
        "kind": "deebotOtaTimestamp",
        "channel": "stable",
        "version": 2,
        "snapshotId": snapshot_id,
        "snapshotPath": f"snapshots/{snapshot_id}/snapshot.json",
        "snapshotSha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "snapshotLength": len(snapshot_bytes),
        "publishedAt": "2026-05-23T00:00:00Z",
        "expiresAt": "2026-06-23T00:00:00Z",
        "signature": "timestamp-signature",
    }
    timestamp_text = json.dumps(timestamp)
    (remote / "timestamp.json").write_text(timestamp_text, encoding="utf-8")

    return {
        "snapshot_id": snapshot_id,
        "asset_name": asset_name,
        "asset_bytes": target_bytes,
        "timestamp": timestamp,
        "timestamp_text": timestamp_text,
        "snapshot": snapshot,
        "snapshot_text": snapshot_text,
    }


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

    def test_config_file_supplies_metadata_verifier_options(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            public_key = root / "metadata.pub"
            ota_center = root / "ota-center"
            config_path = root / "mirror-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "metadata_public_key_file": str(public_key),
                        "ota_center_bin": str(ota_center),
                    },
                ),
                encoding="utf-8",
            )

            parser = mirror.build_parser()
            args = parser.parse_args(["sync", "--config", str(config_path)])
            mirror.apply_config(args)

            self.assertEqual(args.metadata_public_key_file, str(public_key))
            self.assertEqual(args.ota_center_bin, str(ota_center))

    def test_build_metadata_verifier_invokes_ota_center(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            public_key = root / "metadata.pub"
            public_key.write_text("test public key", encoding="utf-8")
            log_path = root / "argv.json"
            fake_ota_center = root / "fake-ota-center"
            fake_ota_center.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json",
                        "import pathlib",
                        "import sys",
                        "args = sys.argv[1:]",
                        "file_path = pathlib.Path(args[args.index('--file') + 1])",
                        "pathlib.Path(%s).write_text(json.dumps({'argv': args, 'file_text': file_path.read_text(encoding='utf-8')}), encoding='utf-8')"
                        % json.dumps(str(log_path)),
                    ],
                )
                + "\n",
                encoding="utf-8",
            )
            os.chmod(fake_ota_center, 0o755)

            verifier = mirror.build_metadata_verifier(str(public_key), str(fake_ota_center))
            verifier("timestamp.json", '{"signed": true}', "ignored-by-wrapper")

            recorded = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertIn("verify-metadata", recorded["argv"])
            self.assertIn("--file", recorded["argv"])
            self.assertIn("--public-key-file", recorded["argv"])
            self.assertIn(str(public_key), recorded["argv"])
            self.assertEqual(recorded["file_text"], '{"signed": true}')

    def test_sync_requires_snapshot_timestamp_metadata(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()

            with self.assertRaisesRegex(RuntimeError, "timestamp metadata"):
                mirror.sync_mirror(
                    remote_base_url=remote.resolve().as_uri(),
                    cache_dir=root / "cache",
                    public_base_url="http://192.168.1.20:18080",
                    timeout=2,
                    metadata_verifier=lambda name, text, signature: None,
                )

    def test_sync_v2_requires_metadata_verifier_when_timestamp_exists(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            timestamp = {
                "schemaVersion": 1,
                "kind": "deebotOtaTimestamp",
                "snapshotId": "20260523T000000Z-v2test",
                "snapshotPath": "snapshots/20260523T000000Z-v2test/snapshot.json",
                "snapshotSha256": "0" * 64,
                "snapshotLength": 1,
                "signature": "timestamp-signature",
            }
            (remote / "timestamp.json").write_text(json.dumps(timestamp), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "metadata verifier is required"):
                mirror.sync_mirror(
                    remote_base_url=remote.resolve().as_uri(),
                    cache_dir=root / "cache",
                    public_base_url="http://192.168.1.20:18080",
                    timeout=2,
                )

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

    def test_valid_relative_metadata_path_rejects_external_urls(self):
        mirror = load_module()

        self.assertTrue(
            mirror.valid_relative_metadata_path(
                "snapshots/20260523T000000Z-v2test/snapshot.json",
            ),
        )
        for path in (
            "https://example.com/snapshot.json",
            "http:snapshot.json",
            "file:///tmp/snapshot.json",
            "//example.com/snapshot.json",
            "/snapshots/snapshot.json",
            r"snapshots\snapshot.json",
            "snapshots/../snapshot.json",
            "snapshots/%2e%2e/snapshot.json",
            "snapshots/%2E%2E/snapshot.json",
            "snapshots/%2Ftmp/snapshot.json",
            "snapshots/%5Ctmp/snapshot.json",
        ):
            with self.subTest(path=path):
                self.assertFalse(mirror.valid_relative_metadata_path(path))

    def test_sync_v2_snapshot_caches_signed_metadata_and_assets_without_legacy_files(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            fixture = write_snapshot_remote(remote)
            connect_tools = {
                "schemaVersion": 1,
                "bundleId": "connect-tools-configs",
                "version": "2026.05.23.1",
                "url": "https://github.com/example/release/connect-tools-configs.dn-ota",
                "sha256": "a" * 64,
            }
            (remote / "connect-tools-configs.json").write_text(
                json.dumps(connect_tools),
                encoding="utf-8",
            )

            verified = []
            result = mirror.sync_mirror(
                remote_base_url=remote.resolve().as_uri(),
                cache_dir=root / "cache",
                public_base_url="http://192.168.1.20:18080",
                timeout=2,
                metadata_verifier=lambda name, text, signature: verified.append((name, text, signature)),
            )

            self.assertEqual(result.snapshot_name, fixture["snapshot_id"])
            self.assertEqual(result.metadata_count, 3)
            self.assertEqual(result.asset_count, 1)
            self.assertEqual(result.download_count, 1)
            self.assertEqual(
                verified,
                [
                    ("timestamp.json", fixture["timestamp_text"], "timestamp-signature"),
                    (
                        f"snapshots/{fixture['snapshot_id']}/snapshot.json",
                        fixture["snapshot_text"],
                        "snapshot-signature",
                    ),
                ],
            )

            snapshot_dir = root / "cache" / "snapshots" / fixture["snapshot_id"]
            self.assertEqual(
                json.loads((snapshot_dir / "timestamp.json").read_text(encoding="utf-8")),
                fixture["timestamp"],
            )
            self.assertEqual(
                json.loads((snapshot_dir / "snapshot.json").read_text(encoding="utf-8")),
                fixture["snapshot"],
            )
            self.assertEqual(
                json.loads((snapshot_dir / "connect-tools-configs.json").read_text(encoding="utf-8")),
                connect_tools,
            )
            self.assertEqual(
                (snapshot_dir / "assets" / fixture["asset_name"]).read_bytes(),
                fixture["asset_bytes"],
            )
            self.assertFalse((snapshot_dir / "latest.json").exists())
            self.assertFalse((snapshot_dir / "manifest.json").exists())

            state = json.loads((root / "cache" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(
                state["metadataFiles"],
                ["connect-tools-configs.json", "snapshot.json", "timestamp.json"],
            )

    def test_v2_resync_same_snapshot_reuses_existing_assets(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            fixture = write_snapshot_remote(remote, snapshot_id="20260523T010000Z-v2test")

            cache_dir = root / "cache"
            first = mirror.sync_mirror(
                remote_base_url=remote.resolve().as_uri(),
                cache_dir=cache_dir,
                public_base_url="http://192.168.1.20:18080",
                timeout=2,
                metadata_verifier=lambda name, text, signature: None,
            )
            second = mirror.sync_mirror(
                remote_base_url=remote.resolve().as_uri(),
                cache_dir=cache_dir,
                public_base_url="http://192.168.1.20:18080",
                timeout=2,
                metadata_verifier=lambda name, text, signature: None,
            )

            self.assertEqual(first.snapshot_name, fixture["snapshot_id"])
            self.assertEqual(second.snapshot_name, fixture["snapshot_id"])
            self.assertFalse(second.changed)
            self.assertEqual(second.download_count, 0)
            self.assertEqual(second.reused_count, 1)

    def test_v2_resync_same_snapshot_does_not_delete_existing_current_on_failure(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            fixture = write_snapshot_remote(remote, snapshot_id="20260523T020000Z-v2test")

            cache_dir = root / "cache"
            first = mirror.sync_mirror(
                remote_base_url=remote.resolve().as_uri(),
                cache_dir=cache_dir,
                public_base_url="http://192.168.1.20:18080",
                timeout=2,
                metadata_verifier=lambda name, text, signature: None,
            )

            changed = write_snapshot_remote(
                remote,
                snapshot_id=fixture["snapshot_id"],
                target_bytes=b"changed package with same snapshot id",
            )

            with self.assertRaisesRegex(ValueError, "existing v2 snapshot"):
                mirror.sync_mirror(
                    remote_base_url=remote.resolve().as_uri(),
                    cache_dir=cache_dir,
                    public_base_url="http://192.168.1.20:18080",
                    timeout=2,
                    metadata_verifier=lambda name, text, signature: None,
                )

            current = json.loads((cache_dir / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(current["snapshot"], first.snapshot_name)
            self.assertEqual(
                (cache_dir / "snapshots" / first.snapshot_name / "assets" / fixture["asset_name"]).read_bytes(),
                fixture["asset_bytes"],
            )
            self.assertFalse((cache_dir / "snapshots" / first.snapshot_name / "assets" / changed["asset_name"]).exists())

    def test_v2_resync_same_snapshot_rejects_stale_optional_metadata(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            fixture = write_snapshot_remote(remote, snapshot_id="20260523T025000Z-v2test")
            connect_tools = {
                "schemaVersion": 1,
                "bundleId": "connect-tools-configs",
                "version": "2026.05.23.1",
            }
            (remote / "connect-tools-configs.json").write_text(
                json.dumps(connect_tools),
                encoding="utf-8",
            )

            cache_dir = root / "cache"
            first = mirror.sync_mirror(
                remote_base_url=remote.resolve().as_uri(),
                cache_dir=cache_dir,
                public_base_url="http://192.168.1.20:18080",
                timeout=2,
                metadata_verifier=lambda name, text, signature: None,
            )
            (remote / "connect-tools-configs.json").unlink()

            with self.assertRaisesRegex(ValueError, "existing v2 snapshot"):
                mirror.sync_mirror(
                    remote_base_url=remote.resolve().as_uri(),
                    cache_dir=cache_dir,
                    public_base_url="http://192.168.1.20:18080",
                    timeout=2,
                    metadata_verifier=lambda name, text, signature: None,
                )

            current = json.loads((cache_dir / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(current["snapshot"], first.snapshot_name)

    def test_serve_mirror_exposes_snapshot_metadata_and_assets_only(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            fixture = write_snapshot_remote(remote, snapshot_id="20260523T030000Z-v2test")

            cache_dir = root / "cache"
            result = mirror.sync_mirror(
                remote_base_url=remote.resolve().as_uri(),
                cache_dir=cache_dir,
                public_base_url="http://127.0.0.1:18080",
                timeout=2,
                metadata_verifier=lambda name, text, signature: None,
            )

            server = mirror.serve_mirror(cache_dir, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(f"{base_url}/timestamp.json", timeout=2) as response:
                    served_timestamp = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(
                    f"{base_url}/snapshots/{result.snapshot_name}/snapshot.json",
                    timeout=2,
                ) as response:
                    served_snapshot = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(
                    f"{base_url}/snapshots/{result.snapshot_name}/assets/{fixture['asset_name']}",
                    timeout=2,
                ) as response:
                    served_asset = response.read()

                self.assertEqual(served_timestamp, fixture["timestamp"])
                self.assertEqual(served_snapshot, fixture["snapshot"])
                self.assertEqual(served_asset, fixture["asset_bytes"])
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(f"{base_url}/latest.json", timeout=2)
                self.assertEqual(error.exception.code, 404)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(f"{base_url}/manifest.json", timeout=2)
                self.assertEqual(error.exception.code, 404)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(f"{base_url}/connect-tools-configs.json", timeout=2)
                self.assertEqual(error.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
