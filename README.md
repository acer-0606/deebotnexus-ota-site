# DeebotNexus OTA Site

This repository is the public static OTA update site for DeebotNexus.

Only publish files that clients are allowed to download:

- `latest.json`
- `manifest.json`
- `release-report.json`
- `DeebotNexus-*.dn-ota`
- `plugins/*.dn-ota`

Never publish private updater payloads, `.dn-plugin` files, signing keys,
decryption keys, CI secrets, or release records that contain local machine paths.

GitHub Pages should serve this repository from the `main` branch and `/` root.

Client endpoints:

```toml
[updater]
tauri_endpoint = "https://fucker-0606.github.io/deebotnexus-ota-site/latest.json"
deebot_manifest_url = "https://fucker-0606.github.io/deebotnexus-ota-site/manifest.json"
```
