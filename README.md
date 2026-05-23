# DeebotNexus OTA 站点

这个仓库是 DeebotNexus 的公开静态 OTA 更新站点。

GitHub Pages 只发布轻量级元数据和可选的人类可读首页：

- `timestamp.json`
- `snapshots/<snapshotId>/snapshot.json`
- `latest.json`
- `manifest.json`
- `connect-tools-configs.json`
- `index.html`
- `README.md`

可分发的 `.dn-ota` 二进制包发布为 GitHub Release assets，不跟随
Pages 分支一起提交。

不要发布私有 updater payload、`.dn-plugin` 文件、签名密钥、解密密钥、
CI secrets，或者包含本机路径的 release record。

GitHub Pages 应从 `main` 分支的仓库根目录发布。

客户端端点：

```toml
[updater]
ota_timestamp_url = "https://acer-0606.github.io/deebotnexus-ota-site/timestamp.json"
snapshot_mode = "preferred"
tauri_endpoint = "https://acer-0606.github.io/deebotnexus-ota-site/latest.json"
deebot_manifest_url = "https://acer-0606.github.io/deebotnexus-ota-site/manifest.json"
```

新客户端优先使用 `timestamp.json -> snapshot.json -> targets`。`latest.json` 和
`manifest.json` 继续保留给旧客户端桥接升级；它们是兼容入口，不提供 v2 的完整
会话固定和镜像保护。

## 局域网本地镜像

开发者可以在本机启动一个局域网 OTA 镜像服务，供同一局域网内的设备下载
OTA。镜像服务会读取公开元数据，下载真实 `.dn-ota` release assets，
按元数据里的 `sha256` 校验文件，再把下载 URL 改写成本机局域网服务地址。

所有下载包和生成后的本地元数据都放在 `.local-ota/`，该目录已被 Git 忽略，
不要提交。同步时会先生成完整快照，全部 `.dn-ota` 下载并校验通过后，才切换
`current.json` 指针：

```text
.local-ota/
  current.json
  state.json
  snapshots/
    20260522T143000Z-ab12cd34/
      latest.json
      manifest.json
      connect-tools-configs.json
      timestamp.json
      snapshot.json
      assets/
        *.dn-ota
```

HTTP 服务每次请求都从 `current.json` 指向的快照读取文件，因此客户端看到的
总是一整套一致的 JSON 和 OTA 包；失败的同步不会切换当前快照。

启动常驻轮询镜像：

```bash
python3 tools/local_ota_mirror.py run --port 18080 --interval 300
```

从父级 DeebotNexus 仓库执行：

```bash
python3 subrepos/ota-site/tools/local_ota_mirror.py run --port 18080 --interval 300
```

服务会暴露：

- `http://<局域网IP>:18080/latest.json`
- `http://<局域网IP>:18080/manifest.json`
- `http://<局域网IP>:18080/connect-tools-configs.json`
- `http://<局域网IP>:18080/timestamp.json`
- `http://<局域网IP>:18080/snapshots/<snapshot-id>/snapshot.json`
- `http://<局域网IP>:18080/snapshots/<snapshot-id>/assets/<name>.dn-ota`

生成后的 JSON 会把 OTA 包 URL 写成带 `<snapshot-id>` 的地址。这样设备即使已经
拿到旧 JSON，服务随后切换到新快照，旧 JSON 里的包 URL 仍然能下载到对应旧快照
中的文件。

桥接升级：旧客户端仍然可以指向镜像服务的 `/latest.json`。当上游已发布 v2
快照元数据时，镜像会缓存快照声明的主程序 `.dn-ota`，并把本地 `latest.json`
里的下载 URL 改写到 `/snapshots/<snapshot-id>/assets/<asset>.dn-ota`。镜像不会
改写 signed `manifest.json`；支持新 manifest/snapshot 流程的客户端应继续按签名
元数据校验。

如果 legacy `latest.json` 指向的包没有出现在 signed snapshot targets 中，镜像
不会替该 URL 下载额外包；旧客户端会继续看到原始 URL。这样可以避免镜像把未被
snapshot 保护的包伪装成本地桥接资产。

如果自动识别的网卡不对，显式指定局域网 IP：

```bash
python3 tools/local_ota_mirror.py run --advertise-host 192.168.1.20 --port 18080
```

也可以直接指定写入本地元数据的完整 base URL：

```bash
python3 tools/local_ota_mirror.py run --base-url http://192.168.1.20:18080
```

一次性同步和只启动服务模式可用于调试：

```bash
python3 tools/local_ota_mirror.py sync --port 18080
python3 tools/local_ota_mirror.py serve --port 18080
```

## 配置文件

命令行参数可以覆盖配置文件。默认配置文件路径是：

```text
.local-ota/config.json
```

也可以显式指定：

```bash
python3 tools/local_ota_mirror.py run --config .local-ota/config.json
```

示例：

```json
{
  "port": 18080,
  "bind": "0.0.0.0",
  "interval": 300,
  "timeout": 30,
  "advertise_host": "192.168.1.20",
  "remote_base_url": "https://acer-0606.github.io/deebotnexus-ota-site",
  "github_proxy": "http://127.0.0.1:7890",
  "github_proxy_username": "proxy-user",
  "github_proxy_password": "proxy-password",
  "metadata_public_key_file": "/path/to/metadata-public-key.hex",
  "ota_center_bin": "ota-center"
}
```

常用字段：

- `interval`：轮询 GitHub Pages 元数据的间隔，单位秒。
- `port`：局域网 OTA HTTP 服务端口。
- `bind`：HTTP 服务绑定地址，默认 `0.0.0.0`。
- `advertise_host`：写入本地元数据 URL 的局域网 IP 或主机名。
- `base_url`：完整覆盖写入本地元数据的 base URL，例如 `http://192.168.1.20:18080`。
- `cache_dir`：本地镜像缓存目录；不设置时使用 ota-site 仓库内的 `.local-ota/`。
- `github_proxy`：仅 GitHub 相关访问使用的代理地址。
- `github_proxy_username` / `github_proxy_password`：代理账号和密码。
- `metadata_public_key_file`：v2 元数据验签公钥文件，内容为 hex 或 base64 编码；
  配置后同步会调用 `ota-center verify-metadata` 验证 `timestamp.json` 和
  `snapshot.json`。
- `ota_center_bin`：`ota-center` 可执行文件路径；不设置时使用 `ota-center`。

`github_proxy` 只用于访问 GitHub Pages、GitHub Release assets 和
`githubusercontent.com` 相关地址；局域网设备访问本机 OTA 服务不会走这个代理。

如果代理地址本身已经包含账号密码，也可以只写一个字段：

```json
{
  "github_proxy": "http://proxy-user:proxy-password@127.0.0.1:7890"
}
```
