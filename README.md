# DeebotNexus OTA Site

This repository is the public static OTA update site for DeebotNexus.

GitHub Pages only publishes lightweight metadata and the optional human-readable index:

- `latest.json`
- `manifest.json`
- `index.html`
- `README.md`

Distributable `.dn-ota` binaries are published as GitHub Release assets instead of
being tracked in the Pages branch.

Never publish private updater payloads, `.dn-plugin` files, signing keys,
decryption keys, CI secrets, or release records that contain local machine paths.

GitHub Pages should serve this repository from the `main` branch and `/` root.

Client endpoints:

```toml
[updater]
tauri_endpoint = "https://acer-0606.github.io/deebotnexus-ota-site/latest.json"
deebot_manifest_url = "https://acer-0606.github.io/deebotnexus-ota-site/manifest.json"
```
