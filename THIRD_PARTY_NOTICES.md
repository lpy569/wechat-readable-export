# Third-party notices

## wechatauto-replica

- Repository: https://github.com/fanyuantaier/wechatauto-replica
- Source revision: `91dc0e1a601013b759b261061d8f7642f736af90` (tag `v1.2.4.4`)
- Original sources: `wechatauto/db.py`, `wechatauto/media.py`
- License: Apache License 2.0
- Included license: `src/wechat_readable_export/_vendor/LICENSE`
- Included provenance and modification notice: `src/wechat_readable_export/_vendor/NOTICE`

`_vendor/db_readonly.py` contains selected and adapted constants, SQLCipher page
verification/decryption, read-only Windows process enumeration helpers and
Config.Cipher key scanning. The full upstream package is not imported or
installed. Its GUI, message-sending, process-writing, key-cache, automatic
account-switching, UI-driven media download and listener features are not included.

`image_codec.py` adapts local image container decoding and read-only image-key
candidate scanning from `wechatauto/media.py` at the same fixed revision. It adds
strict segment, padding and Pillow raster validation, bounded single-pass
scanning, and no UI, key files, monitoring or persistent caches. `wxgf_codec.py`
uses the same source's Annex B HEVC extraction approach, adding bounded local
FFmpeg decoding and raster validation. Existing memory access is query/read only.

The surrounding account selection, snapshot/WAL validation, CLI, message model,
rendering and tests in this repository are maintained independently. Changes
to upstream layout anchors must be reviewed and validated before updating the
vendored module.

## wechat-chat-export — design reference

- Repository: https://github.com/zhuzhangxue/wechat-chat-export
- Reviewed revision: `2c49469a91e2f3b0c73a581e15a8ad2db25b7923`
- License: Apache License 2.0

Its exporter informed the initial workflow and the handling of per-shard sender
identities, message formats, disposable caches, resource file hashes and local
image/thumbnail paths. `local_media.py` implements the resource lookup and local
path handling independently, with unambiguous matching and containment checks.
This project does not bundle
its full exporter or its application UI. The current parser and renderers are
separate implementations.

## Native emoji rendering

`native_emoji.py` renders validated PNG tiles supplied by a per-user local cache
or an imported export's metadata. This module contains no emoji artwork and makes
no network requests. Artwork caches and real exports are not part of the source
distribution.

## Runtime dependencies

- `cryptography`: https://github.com/pyca/cryptography — Apache-2.0 OR BSD-3-Clause
- `zstandard`: https://github.com/indygreg/python-zstandard — BSD-3-Clause
- `Pillow`: https://github.com/python-pillow/Pillow — MIT-CMU (see the installed distribution for bundled-library licenses)
- `imageio-ffmpeg`: https://github.com/imageio/imageio-ffmpeg — BSD-2-Clause for the Python wrapper. Its separately installed wheel includes an FFmpeg executable; the Windows 0.6.0 wheel's FFmpeg 7.1 build reports GPL-3.0-or-later. See the dependency's bundled license and https://ffmpeg.org/legal.html for executable licensing and source requirements. This repository does not copy that executable into its source distribution.

These dependencies are installed separately by the package manager. Their own
distributed license files govern their code and any bundled native libraries.
