import hashlib
import importlib.util
import json
import os
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

    def test_sync_v2_snapshot_caches_assets_and_only_rewrites_legacy_latest(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            snapshot_id = "20260523T000000Z-v2test"
            snapshot_remote_dir = remote / "snapshots" / snapshot_id
            snapshot_remote_dir.mkdir(parents=True)

            asset_bytes = b"snapshot ota package"
            asset_sha = hashlib.sha256(asset_bytes).hexdigest()
            asset_name = f"sha256-{asset_sha[:12]}-DeebotNexus-main-macos-2.0.0.dn-ota"
            asset = remote / asset_name
            asset.write_bytes(asset_bytes)
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
                        "length": len(asset_bytes),
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

            latest = {
                "version": "2.0.0",
                "platforms": {
                    "darwin-aarch64": {
                        "url": asset_url,
                        "sha256": asset_sha,
                        "signature": "legacy-signature",
                    },
                },
            }
            manifest = {
                "schemaVersion": 1,
                "channel": "stable",
                "app": {
                    "version": "2.0.0",
                    "platforms": {
                        "darwin-aarch64": {
                            "url": asset_url,
                            "sha256": asset_sha,
                            "tauriSignature": "legacy-signature",
                        },
                    },
                },
                "plugins": [],
                "signature": "manifest-signature",
            }
            (remote / "latest.json").write_text(json.dumps(latest), encoding="utf-8")
            (remote / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            cache_dir = root / "cache"
            verified = []
            result = mirror.sync_mirror(
                remote_base_url=remote.resolve().as_uri(),
                cache_dir=cache_dir,
                public_base_url="http://192.168.1.20:18080",
                timeout=2,
                metadata_verifier=lambda name, text, signature: verified.append((name, text, signature)),
            )

            self.assertEqual(result.snapshot_name, snapshot_id)
            self.assertEqual(
                verified,
                [
                    ("timestamp.json", timestamp_text, "timestamp-signature"),
                    (f"snapshots/{snapshot_id}/snapshot.json", snapshot_text, "snapshot-signature"),
                ],
            )

            snapshot_dir = cache_dir / "snapshots" / snapshot_id
            self.assertEqual(
                json.loads((snapshot_dir / "timestamp.json").read_text(encoding="utf-8")),
                timestamp,
            )
            self.assertEqual(
                json.loads((snapshot_dir / "snapshot.json").read_text(encoding="utf-8")),
                snapshot,
            )
            self.assertEqual(
                (snapshot_dir / "assets" / asset_name).read_bytes(),
                asset_bytes,
            )

            local_latest = json.loads((snapshot_dir / "latest.json").read_text(encoding="utf-8"))
            expected_url = f"http://192.168.1.20:18080/snapshots/{snapshot_id}/assets/{asset_name}"
            self.assertEqual(local_latest["platforms"]["darwin-aarch64"]["url"], expected_url)
            self.assertEqual(local_latest["platforms"]["darwin-aarch64"]["signature"], "legacy-signature")
            self.assertEqual(local_latest["platforms"]["darwin-aarch64"]["sha256"], asset_sha)

            local_manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(local_manifest, manifest)
            self.assertEqual(
                local_manifest["app"]["platforms"]["darwin-aarch64"]["url"],
                asset_url,
            )

    def test_v2_resync_same_snapshot_does_not_delete_existing_current_on_failure(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            snapshot_id = "20260523T010000Z-v2test"
            snapshot_remote_dir = remote / "snapshots" / snapshot_id
            snapshot_remote_dir.mkdir(parents=True)

            asset_bytes = b"stable signed package"
            asset_sha = hashlib.sha256(asset_bytes).hexdigest()
            asset_name = f"sha256-{asset_sha[:12]}-DeebotNexus-main-macos-2.0.1.dn-ota"
            asset = remote / asset_name
            asset.write_bytes(asset_bytes)
            asset_url = asset.resolve().as_uri()

            snapshot = {
                "schemaVersion": 1,
                "kind": "deebotOtaSnapshot",
                "snapshotId": snapshot_id,
                "channel": "stable",
                "version": 2,
                "publishedAt": "2026-05-23T01:00:00Z",
                "minimumHostVersion": "0.1.0",
                "metadata": [],
                "targets": [
                    {
                        "targetId": "app:darwin-aarch64:2.0.1",
                        "kind": "app",
                        "platform": "darwin-aarch64",
                        "version": "2.0.1",
                        "assetName": asset_name,
                        "sha256": asset_sha,
                        "length": len(asset_bytes),
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
                "publishedAt": "2026-05-23T01:00:00Z",
                "expiresAt": "2026-06-23T01:00:00Z",
                "signature": "timestamp-signature",
            }
            (remote / "timestamp.json").write_text(json.dumps(timestamp), encoding="utf-8")
            (remote / "latest.json").write_text(
                json.dumps(
                    {
                        "version": "2.0.1",
                        "platforms": {
                            "darwin-aarch64": {
                                "url": asset_url,
                                "sha256": asset_sha,
                                "signature": "legacy-signature",
                            },
                        },
                    },
                ),
                encoding="utf-8",
            )
            (remote / "manifest.json").write_text(
                json.dumps({"schemaVersion": 1, "app": {"platforms": {}}, "plugins": []}),
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

            changed_bytes = b"changed package with same snapshot id"
            changed_sha = hashlib.sha256(changed_bytes).hexdigest()
            changed_name = f"sha256-{changed_sha[:12]}-DeebotNexus-main-macos-2.0.1.dn-ota"
            changed_asset = remote / changed_name
            changed_asset.write_bytes(changed_bytes)
            changed_url = changed_asset.resolve().as_uri()
            snapshot["targets"][0]["assetName"] = changed_name
            snapshot["targets"][0]["sha256"] = changed_sha
            snapshot["targets"][0]["length"] = len(changed_bytes)
            snapshot["targets"][0]["locations"] = [
                {"kind": "githubRelease", "url": changed_url},
                {"kind": "snapshotMirror", "path": f"assets/{changed_name}"},
            ]
            changed_snapshot_text = json.dumps(snapshot, sort_keys=True)
            changed_snapshot_bytes = changed_snapshot_text.encode("utf-8")
            (snapshot_remote_dir / "snapshot.json").write_text(changed_snapshot_text, encoding="utf-8")
            timestamp["snapshotSha256"] = hashlib.sha256(changed_snapshot_bytes).hexdigest()
            timestamp["snapshotLength"] = len(changed_snapshot_bytes)
            (remote / "timestamp.json").write_text(json.dumps(timestamp), encoding="utf-8")
            (remote / "latest.json").write_text(
                json.dumps(
                    {
                        "version": "2.0.1",
                        "platforms": {
                            "darwin-aarch64": {
                                "url": changed_url,
                                "sha256": changed_sha,
                                "signature": "legacy-signature",
                            },
                        },
                    },
                ),
                encoding="utf-8",
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
                (cache_dir / "snapshots" / first.snapshot_name / "assets" / asset_name).read_bytes(),
                asset_bytes,
            )

    def test_v2_bridge_latest_does_not_cache_unsigned_asset_outside_snapshot(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            snapshot_id = "20260523T020000Z-v2test"
            snapshot_remote_dir = remote / "snapshots" / snapshot_id
            snapshot_remote_dir.mkdir(parents=True)

            signed_bytes = b"signed package"
            signed_sha = hashlib.sha256(signed_bytes).hexdigest()
            signed_name = f"sha256-{signed_sha[:12]}-signed.dn-ota"
            signed_asset = remote / signed_name
            signed_asset.write_bytes(signed_bytes)
            signed_url = signed_asset.resolve().as_uri()

            unsigned_bytes = b"unsigned package"
            unsigned_sha = hashlib.sha256(unsigned_bytes).hexdigest()
            unsigned_asset = remote / "unsigned.dn-ota"
            unsigned_asset.write_bytes(unsigned_bytes)
            unsigned_url = unsigned_asset.resolve().as_uri()

            snapshot = {
                "schemaVersion": 1,
                "kind": "deebotOtaSnapshot",
                "snapshotId": snapshot_id,
                "channel": "stable",
                "version": 2,
                "publishedAt": "2026-05-23T02:00:00Z",
                "minimumHostVersion": "0.1.0",
                "metadata": [],
                "targets": [
                    {
                        "targetId": "app:darwin-aarch64:2.0.2",
                        "kind": "app",
                        "platform": "darwin-aarch64",
                        "version": "2.0.2",
                        "assetName": signed_name,
                        "sha256": signed_sha,
                        "length": len(signed_bytes),
                        "locations": [
                            {"kind": "githubRelease", "url": signed_url},
                            {"kind": "snapshotMirror", "path": f"assets/{signed_name}"},
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
                "publishedAt": "2026-05-23T02:00:00Z",
                "expiresAt": "2026-06-23T02:00:00Z",
                "signature": "timestamp-signature",
            }
            (remote / "timestamp.json").write_text(json.dumps(timestamp), encoding="utf-8")
            (remote / "latest.json").write_text(
                json.dumps(
                    {
                        "version": "2.0.2",
                        "platforms": {
                            "darwin-aarch64": {
                                "url": unsigned_url,
                                "sha256": unsigned_sha,
                                "signature": "legacy-signature",
                            },
                        },
                    },
                ),
                encoding="utf-8",
            )
            (remote / "manifest.json").write_text(
                json.dumps({"schemaVersion": 1, "app": {"platforms": {}}, "plugins": []}),
                encoding="utf-8",
            )

            result = mirror.sync_mirror(
                remote_base_url=remote.resolve().as_uri(),
                cache_dir=root / "cache",
                public_base_url="http://192.168.1.20:18080",
                timeout=2,
                metadata_verifier=lambda name, text, signature: None,
            )

            snapshot_dir = root / "cache" / "snapshots" / result.snapshot_name
            self.assertTrue((snapshot_dir / "assets" / signed_name).exists())
            self.assertFalse((snapshot_dir / "assets" / unsigned_asset.name).exists())
            local_latest = json.loads((snapshot_dir / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(local_latest["platforms"]["darwin-aarch64"]["url"], unsigned_url)

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
