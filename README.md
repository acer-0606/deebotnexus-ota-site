# DeebotNexus OTA 站点

这个仓库是 DeebotNexus 的公开静态 OTA 更新站点。

GitHub Pages 只发布轻量级元数据和可选的人类可读首页：

- `timestamp.json`
- `snapshots/<snapshotId>/snapshot.json`
- `connect-tools-configs.json`（可选）
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
```

客户端只使用 `timestamp.json -> snapshot.json -> targets`。公开站点不再提供
`latest.json` 或 `manifest.json` 兼容入口；主程序、插件和高级工具配置都通过
snapshot targets 独立声明和校验。

## 局域网本地镜像

开发者可以在本机启动一个局域网 OTA 镜像服务，供同一局域网内的设备下载
OTA。镜像服务会读取公开元数据，下载真实 `.dn-ota` release assets，
按 snapshot target 里的 `sha256` 和 `length` 校验文件。snapshot 中保留
`snapshotMirror` 相对路径，本地服务通过
`/snapshots/<snapshot-id>/assets/...` 提供这些文件。

所有下载包和生成后的本地元数据都放在 `.local-ota/`，该目录已被 Git 忽略，
不要提交。同步时会先生成完整快照，全部 `.dn-ota` 下载并校验通过后，才切换
`current.json` 指针：

```text
.local-ota/
  current.json
  state.json
  snapshots/
    20260522T143000Z-ab12cd34/
      timestamp.json
      snapshot.json
      connect-tools-configs.json
      assets/
        *.dn-ota
```

HTTP 服务每次请求都从 `current.json` 指向的快照读取文件，因此客户端看到的
总是一整套一致的 JSON 和 OTA 包；失败的同步不会切换当前快照。

如果公开站点仍保留可选的 `connect-tools-configs.json` 高级工具配置指针，
本地镜像会同时下载该指针里的 `.dn-ota` 包，按 `sha256` 校验后放入当前
snapshot 的 `assets/` 目录。客户端访问镜像的
`/connect-tools-configs.json` 时，服务会把指针里的 `url` 动态改写为本机
镜像地址，避免高级工具配置包下载绕回 GitHub Release。

启动常驻轮询镜像：

```bash
python3 tools/local_ota_mirror.py run --port 18080 --interval 300
```

从父级 DeebotNexus 仓库执行：

```bash
python3 subrepos/ota-site/tools/local_ota_mirror.py run --port 18080 --interval 300
```

服务会暴露：

- `http://<局域网IP>:18080/timestamp.json`
- `http://<局域网IP>:18080/connect-tools-configs.json`（可选）
- `http://<局域网IP>:18080/snapshots/<snapshot-id>/snapshot.json`
- `http://<局域网IP>:18080/snapshots/<snapshot-id>/assets/<name>.dn-ota`

snapshot target 中的 `snapshotMirror` 是 `assets/<name>.dn-ota` 相对路径。本地
镜像按当前 snapshot URL 所在目录解析它，因此最终下载地址会包含
`/snapshots/<snapshot-id>/assets/...`。这样设备即使已经拿到旧 `snapshot.json`，
服务随后切换到新快照，旧 snapshot 里的包路径仍然能下载到对应旧快照中的文件。

本地镜像会缓存 snapshot targets 中声明的 `.dn-ota` 文件，并按 target 里的
`sha256` 和 `length` 校验；可选高级工具配置指针中的 `.dn-ota` 也会按指针
里的 `sha256` 校验后缓存。`/latest.json` 和 `/manifest.json` 会返回 404，避免
旧入口绕过 snapshot 固定和目标级校验。

如果自动识别的网卡不对，显式指定局域网 IP：

```bash
python3 tools/local_ota_mirror.py run --advertise-host 192.168.1.20 --port 18080
```

也可以直接指定展示给客户端的完整镜像服务 base URL：

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
  "metadata_public_key_file": "/path/to/metadata-public-key.hex"
}
```

常用字段：

- `interval`：轮询 GitHub Pages 元数据的间隔，单位秒。
- `port`：局域网 OTA HTTP 服务端口。
- `bind`：HTTP 服务绑定地址，默认 `0.0.0.0`。
- `advertise_host`：展示给局域网设备访问本机镜像服务的 IP 或主机名。
- `base_url`：完整覆盖展示给客户端的镜像服务 base URL，例如 `http://192.168.1.20:18080`。
- `cache_dir`：本地镜像缓存目录；不设置时使用 ota-site 仓库内的 `.local-ota/`。
- `github_proxy`：仅 GitHub 相关访问使用的代理地址。
- `github_proxy_username` / `github_proxy_password`：代理账号和密码。
- `metadata_public_key_file`：v2 元数据验签公钥文件，内容为 hex 或 base64 编码；
  同步会用它直接验证 `timestamp.json` 和 `snapshot.json` 的签名。

`github_proxy` 只用于访问 GitHub Pages、GitHub Release assets 和
`githubusercontent.com` 相关地址；局域网设备访问本机 OTA 服务不会走这个代理。

如果代理地址本身已经包含账号密码，也可以只写一个字段：

```json
{
  "github_proxy": "http://proxy-user:proxy-password@127.0.0.1:7890"
}
```
