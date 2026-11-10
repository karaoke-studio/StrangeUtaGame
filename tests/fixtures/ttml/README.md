# TTML 测试样本

本目录保留 SUG TTML 交换格式的回归样本。真实样本来自
[AMLL TTML DB](https://github.com/amll-dev/amll-ttml-db) 各平台分区，
测试只把文件当作解析/回环样本，不再分发音频。

| 文件 | 来源 | SHA-256 | 用途 |
|---|---|---|---|
| `sug_roundtrip.ttml` | SUG 自建 | `012c27f41caf858193faa6728ad008b182eca638d5a989eafe4859c844852c0c` | 覆盖逐词时间轴、多字 span、TTML2 ruby、双演唱者、行尾间隙、句中停顿（`x-sug-release`）与连词（`x-sug-link`）。 |
| `amll_101126.ttml` | [AMLL TTML DB `ncm-lyrics/101126.ttml`](https://github.com/amll-dev/amll-ttml-db/blob/main/ncm-lyrics/101126.ttml) | `475f8965a75613066b93bc1f5ddc3d20c9ee22aca3a876ca40a94d9e1c34fcf9` | 真实 AMLL TTML DB 文件（《第三十八年夏至》，河图），双无名 agent、逐字时间轴、无 ruby/翻译/背景和声，用于真实文件回环。 |
| `amll_ncm_2020905278.ttml` | [`ncm-lyrics/2020905278.ttml`](https://github.com/amll-dev/amll-ttml-db/blob/main/ncm-lyrics/2020905278.ttml) | `c83c43fb1effdf33dfc5987732ab185a1d7cc7467f0acafb8c7157dba9de4acb` | 网易云分区；双无名 agent（多 artists）、逐行 `x-translation` + `x-roman`，覆盖翻译/音译与多艺人 metadata 回写。 |
| `amll_qq_002dB4xY3S3gCr.ttml` | [`qq-lyrics/002dB4xY3S3gCr.ttml`](https://github.com/amll-dev/amll-ttml-db/blob/main/qq-lyrics/002dB4xY3S3gCr.ttml) | `e2eba0d867c502888ada28e55180366442b840667954c80a18bb792bcd16eba2` | QQ 音乐分区；具名 `person` + 同名 `group` agent、`x-bg`（内嵌 `x-translation`）逐词子 span，覆盖 agent type/同名去重与背景人声。 |
| `amll_am_1019650261.ttml` | [`am-lyrics/1019650261.ttml`](https://github.com/amll-dev/amll-ttml-db/blob/main/am-lyrics/1019650261.ttml) | `78a5984fb3c0fc0471d54be0acd94267ec93e89b5146bbbe7ed0599dbc8b9614` | Apple Music 分区；带逐词子 span 的 `x-bg`，覆盖嵌套背景和声时间轴。 |
| `amll_spotify_6nFn8VpgIi1KJbowtM8JRs.ttml` | [`spotify-lyrics/6nFn8VpgIi1KJbowtM8JRs.ttml`](https://github.com/amll-dev/amll-ttml-db/blob/main/spotify-lyrics/6nFn8VpgIi1KJbowtM8JRs.ttml) | `b2ec14538f7dfcd9c25251e25b197704298c1a52c37c1d2d2dbe4e561bfae2cb` | Spotify 分区；双无名 agent、`x-translation` + `x-roman` + `x-bg` 三种辅助轨。 |
| `amll_raw_1763204456985-50747104-2f658529.ttml` | [`raw-lyrics/1763204456985-50747104-2f658529.ttml`](https://github.com/amll-dev/amll-ttml-db/blob/main/raw-lyrics/1763204456985-50747104-2f658529.ttml) | `1be65c6556244ffb145b322e5d8e0f3df85f1e160af89048aa996269323a8934` | raw-lyrics 分区；`x-translation` + `x-roman` + `x-bg`，覆盖历史投稿文件。 |

`amll_*` 样本版权/共享方式遵循原仓库：外来数据部分遵循原数据提供方协议，
提交者自主编写的部分为 CC0 1.0。测试只做解析/回环，不再分发音频。

更新真实样本时请用仓库 raw 文件的原字节写入并同步更新 SHA-256；不要用
会转换换行的编辑器改写。
