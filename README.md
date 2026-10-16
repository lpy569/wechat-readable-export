# 微信聊天记录 → 可阅读文件

把 **Windows 微信 4.x 本机保存的一个会话** 导出为可离线阅读、搜索和存档的文件。

- **HTML**：离线阅读、聊天文字搜索、筛选、打印；展示可读取的图片和动画表情，点击图片可放大。
- **TXT**：逐条保留北京时间、发送人和可解析文字。
- **JSON**：保留消息类型、来源分片、发送者映射状态、导出诊断和已恢复的图片/表情数据。
- 支持私聊、群聊、指定日期范围，以及同一会话跨多个数据库分片的记录。
- 提供虚构数据演示和 JSON 重新转换功能，不打开微信也可以体验。

这是一个独立开源项目，与腾讯无隶属关系。v0.2.0 支持**文字、可读取的本地图片和表情展示**。语音、视频和附件仍保留文字标记；不下载图片原件、不新增语音识别。无法定位或解码的图片/表情会保留原因提示。

![离线阅读页，全部内容为虚构演示数据](docs/preview.png)

## 快速体验

需要 Python 3.10 或更高版本。以下为 Windows PowerShell 命令；无需激活虚拟环境。

```powershell
cd D:\Code\wechat-readable-export
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\wechat-export.exe demo --output demo-output
Start-Process .\demo-output\chat.html
```

演示中的人名、ID 和内容均为虚构。macOS / Linux 也可以执行 `python -m wechat_readable_export demo`；读取真实微信数据库仅支持 Windows。

## 导出自己的聊天

### 1. 检查环境

打开并登录电脑版微信，保持微信运行：

```powershell
.\.venv\Scripts\wechat-export.exe doctor
.\.venv\Scripts\wechat-export.exe accounts
```

程序优先探测当前用户的 `Documents\xwechat_files`。如果微信数据保存在其他位置，请提供 `--data-dir`。可在微信设置中的存储位置查找；可以传 `xwechat_files` 根目录，也可以传包含 `db_storage` 的具体账号目录。

```powershell
.\.venv\Scripts\wechat-export.exe accounts --data-dir "E:\微信数据\xwechat_files"
```

发现多个账号时必须用 `--account` 指定目录名称，程序不会根据修改时间猜测账号。

### 2. 查询联系人或群聊

```powershell
.\.venv\Scripts\wechat-export.exe contacts --search "林同学"
```

输出为 `ID / 昵称 / 备注`。复制目标的唯一 ID；私聊通常以 `wxid_` 开头，群聊 ID 通常以 `@chatroom` 结尾。

```powershell
.\.venv\Scripts\wechat-export.exe contacts --data-dir "E:\微信数据\xwechat_files" --account "wxid_example_abcd" --search "读书群"
```

### 3. 导出指定会话

```powershell
.\.venv\Scripts\wechat-export.exe export --contact-id "wxid_example_friend" --output "exports\friend"
```

也可以直接按昵称或备注查询；存在多个匹配时会停止并列出候选 ID。

```powershell
.\.venv\Scripts\wechat-export.exe export --contact "林同学" --output "exports\friend"
```

只导出某个日期范围、只生成部分格式：

```powershell
.\.venv\Scripts\wechat-export.exe export --contact-id "wxid_example_friend" --start 2026-01-01 --end 2026-01-31 --format html txt --output "exports\january"
```

`--start` 和 `--end` 都按北京时间解释，**包含起止两天**。不传日期参数则读取该会话在本机可读取的全部记录。

### 图片与表情

默认尝试读取所选会话的本地图片：优先按资源数据库的文件哈希定位，依次尝试本地原图副本、普通副本和缩略图。支持可验证的 JPEG、PNG、GIF、WebP 及部分微信 `.dat` 加密格式；wxgf/HEVC 图片通过本地 FFmpeg 解码，保留像素尺寸，不做放大处理。需要时仅只读扫描一次微信进程取得图片解密材料。未找到或不支持的格式会显示提示；缩略图会明确标注，不能保证每条图片都有原图。HTML 中成功显示的图片不重复显示“图片”和“[图片]”占位文字，点击图片可放大或缩小。

普通 Unicode emoji（如 😊）会原样显示。微信 `[捂脸]`、`[微笑]` 等内置表情在有本地素材缓存或 JSON 内嵌素材时，会在 HTML 中显示为行内图案，仍可按原标签搜索；TXT 和 JSON 的消息正文保留原始文字。未收录的标签继续显示原文。

Windows 的内置表情素材缓存位于 `%LOCALAPPDATA%\wechat-readable-export\native-emoji-v1.json`（其他系统使用 XDG 或用户 `.cache`）。缓存采用 `{schema_version:1, source:字符串, assets:{标签:PNG媒体对象}}`；仅接受经过大小、格式与尺寸校验的 PNG。导出只内嵌用到的图案，JSON 转换时不依赖原机器缓存。本项目不随源码分发表情图片，导出与转换过程也不会自动联网获取内置表情素材。

自定义动画表情包与这些内置表情分开处理：会先尝试本地哈希命名缓存，无法保证所有微信版本的缓存布局都兼容。若本地没有表情包，可以明确允许从消息中记录的微信 CDN 地址获取：

```powershell
.\.venv\Scripts\wechat-export.exe export --contact-id "wxid_example_friend" --download-stickers --output "exports\with-media"
```

`--download-stickers` 只影响缺失表情：限制微信 CDN 域名、校验重定向、大小、格式和可用的 MD5。地址过期、返回加密数据或文件不支持时仍保留提示。**不传此参数不会请求网络**。只要文字、不读取媒体时使用 `--no-media`；它不能与 `--download-stickers` 同时使用。

图片和表情以 Base64 嵌入 HTML 和 JSON，不依赖旁边的媒体目录。GIF/WebP 的动画会保留，复制单个 HTML 就能离线阅读；JSON 重新转换后也保留媒体。嵌入会增加文件体积，每个媒体限制 32 MiB，并限制图片尺寸和动画总像素；大量图片的导出会占用更多内存。TXT 保留文字标记和缺失原因。

### 输出内容

```text
exports/friend/
├── chat.html
├── chat.txt
└── chat.json
```

所有时间使用北京时间（UTC+08:00）。默认拒绝覆盖已有同名文件；选择新的 `--output`，或明确使用 `--overwrite`。

JSON 可以在不打开微信时重新生成阅读文件：

```powershell
.\.venv\Scripts\wechat-export.exe convert "exports\friend\chat.json" --format html txt --output "exports\recreated"
```

`convert` 只接受本项目 `schema_version=1` 的 JSON，不接受任意聊天软件的 JSON。兼容旧版不含媒体的 JSON，但旧文件只有占位文字时，无法凭空补回已丢失的图片线索，需要从微信重新导出。

## 数据处理方式

程序只对 `Weixin.exe` 申请查询与只读内存访问，用于取得当前登录会话的数据库解密材料；密钥只留在本次进程内存中。它不注入 DLL、不写微信进程、不修改原数据库、不自动重启微信。

读取联系人时仅处理联系人数据库；导出时再处理消息数据库分片，需要图片时额外读取图片资源数据库。解密副本位于系统临时目录，操作结束或正常异常退出时清理。SQLite 只读查询副本，发送者 ID 在各自分片的 `Name2Id` 中解析。消息数据库校验失败会停止，不用猜测的密钥或发送者替代。图片资源或媒体无法读取时保留明确提示，不中断已可读取的文字。未知消息类型和未映射身份会保留并产生提示。

默认导出不发送聊天数据；生成的 HTML 不依赖 CDN、远程字体或网络服务。显式指定 `--download-stickers` 时，程序只向允许的微信 CDN 请求消息里的表情地址，地址中的原始查询参数会随该请求发送；不上传聊天正文、数据库或密钥。安装 Python 依赖需要联网。操作系统强制结束进程或断电可能使临时目录来不及清理，见 [排错说明](docs/TROUBLESHOOTING.md)。

`.gitignore` 排除了默认导出目录、`chat.*`、数据库和常见密钥文件。**它不能阻止你主动把改名后的真实聊天加入 Git**；发布前请检查 `git status` 和待提交内容。

## 支持范围与限制

- 读取 Windows 微信 **4.x** 的当前本地数据库；微信升级可能改变加密结构，需要重新验证。
- 已在 Windows 微信 **4.1.15.51** 完成一次真实本地读取核对，具体环境和验证边界见 [验证记录](docs/VALIDATION.md)。
- 手机独有、未同步到电脑、已删除或本机不存在的记录不在导出范围内。
- 不包含恢复已删除聊天、语音/视频/附件原文件导出、在线账号登录、云端上传或微信消息发送。
- 图片与表情必须已在本机保存且格式兼容，或能通过显式启用的表情 CDN 获取。wxgf/HEVC 解码器不可用、文件损坏或超过解码限制时，会尝试本地缩略图。测试使用合成素材；真实导出文件不进入源码和测试数据。
- 原生语音转文字仅在本机记录中确实存在且格式可解析时保留。
- 原数据库或 WAL 正在变化、页认证失败或快照无法确认时会报错；稍后重试。WAL 预分配空间和可验证的旧世代数据帧不作为新消息应用。
- 数据库结构兼容性、验证状态与具体诊断见 `doctor` 输出、导出 JSON 的 `metadata` 和终端提示。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip wheel . --no-deps --wheel-dir dist
```

测试只使用合成数据，不需要微信和真实聊天。GitHub Actions 会在 Windows / Ubuntu 上执行测试；这不代表自动验证了所有微信版本。

```text
src/wechat_readable_export/
├── cli.py          # 参数、联系人唯一性、命令分发
├── backend.py      # Windows 只读读取与快照验证
├── parsing.py      # 消息解析、分片身份、排序、日期过滤
├── rendering.py    # 离线 HTML / TXT / JSON
├── local_media.py  # 会话媒体定位、缺失提示、可选表情 CDN
├── image_codec.py  # 本地图片解码与只读图片密钥扫描
├── wxgf_codec.py   # 本地 HEVC 高清图片解码，保持像素尺寸
├── media_payload.py # 便携媒体数据与格式/尺寸校验
├── native_emoji.py # 内置表情的离线行内渲染，保留原标签
├── models.py       # 共享数据模型
├── demo.py         # 虚构演示数据
└── _vendor/        # 固定来源的最小数据库读取代码
tests/
```

## 自己推送到 GitHub

在 GitHub 建立一个空仓库，然后执行：

```powershell
git init -b main
git add .
git status --short
git commit -m "Initial release: readable WeChat export"
git remote add origin https://github.com/YOUR_USERNAME/wechat-readable-export.git
git push -u origin main
```

不要将真实导出文件用作示例或问题附件。提交问题时可提供微信版本、Python 版本、异常类型和不含个人信息的诊断；无需提供数据库或密钥。

## 许可与致谢

本项目使用 [Apache License 2.0](LICENSE)。数据库读取与部分消息解析实现参考并改编自 `wechatauto-replica` 和 `wechat-chat-export`，固定提交、修改说明及许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
