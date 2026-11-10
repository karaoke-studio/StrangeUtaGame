# TTML 测试样本

本目录只保留两个用于 SUG TTML 前置工作的样本：

| 文件 | 来源 | SHA-256 | 用途 |
|---|---|---|---|
| `sug_roundtrip.ttml` | SUG 自建 | `012c27f41caf858193faa6728ad008b182eca638d5a989eafe4859c844852c0c` | 覆盖逐词时间轴、多字 span、TTML2 ruby、双演唱者、行尾间隙、句中停顿（`x-sug-release`）与连词（`x-sug-link`）。 |
| `amll_101126.ttml` | [AMLL TTML DB `ncm-lyrics/101126.ttml`](https://github.com/amll-dev/amll-ttml-db/blob/main/ncm-lyrics/101126.ttml) | `475f8965a75613066b93bc1f5ddc3d20c9ee22aca3a876ca40a94d9e1c34fcf9` | 真实 AMLL TTML DB 文件（《第三十八年夏至》，河图），双 agent、逐字时间轴、无 ruby/翻译/背景和声，用于真实文件回环。 |

`amll_101126.ttml` 版权/共享方式遵循原仓库：外来数据部分遵循原数据提供方协议，
提交者自主编写的部分为 CC0 1.0。测试只把它当作解析样本，不再分发音频。
