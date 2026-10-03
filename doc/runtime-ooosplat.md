<!-- SPDX-License-Identifier: BSD-3-Clause -->
# 在 OOOSplat 或其他开源项目中接入

本文件只维护在 COLMAP fork 内，不改变 OOOSplat 的正式清单、源码或本地命令。
使用者先按 [下载说明](runtime-distribution.md) 校验并解压完整运行包。

Windows 可手动解压到 OOOSplat 既有 `engines/colmap`，形成
`engines/colmap/bin/colmap.exe`，保留所有 DLL、lib、许可证和元数据。
应在自己的开发副本中备份原目录，再整体切换，避免混用新旧 DLL；不要直接将此包
的哈希或 CUDA 13.2 信息写进 OOOSplat 正式发行清单。原清单可能锁定其他 COLMAP、
CUDA 或 ONNX 文件，正式发行校验失败需要后续单独评审接入变更。

此运行包额外包含闭环需要的离线 SIFT 词汇树：
`lib/colmap/vocab_tree_faiss_flickr100K_words256K.bin`，Windows 解压后为
`engines/colmap/lib/colmap/vocab_tree_faiss_flickr100K_words256K.bin`。
直接调用 COLMAP 时把该路径传入 `SequentialMatching.vocab_tree_path` 并开启
`SequentialMatching.loop_detection`；词汇树匹配使用 `VocabTreeMatching.vocab_tree_path`。
当前 OOOSplat 有排除旧词汇树资源的正式打包规则，目录和调用是否纳入其正式发行
仍须独立评审。这里仅保证此 fork 的三平台归档要求包含并验收离线树，
不修改或绕过 OOOSplat 正式资源清单，不声称 OOOSplat 已自动接上闭环功能。

Linux 的既有命令解析支持 `OOOSPLAT_COLMAP`，可在启动本地开发进程前设置：

```sh
export OOOSPLAT_COLMAP="/绝对路径/colmap-runtime/bin/colmap"
```

也可在已有 PATH 解析约定允许时把该包的 `bin` 加入 PATH。运行库保持在原包位置，
不要只复制或孤立链接入口。不要因此改写 OOOSplat 的本地启动命令。

macOS 既有混合运行时通过 `engines/macos/arm64/bin/colmap` 等目录解析，还含
FFmpeg/FFprobe、Brush 及独立发行元数据。本项目的 macOS 包仅提供 COLMAP，
不等于 OOOSplat 的混合运行时，不能拿它整体替换 `engines/macos/arm64` 并声称
OOOSplat 完整可运行。独立开发者可直接使用 `colmap-runtime/bin/colmap`；若要接入
现有混合运行时，应另行审核库冲突、系统要求、签名及正式清单，而非复制一个可执行文件。

手动本地开发继续遵循 OOOSplat 的免归档校验约定；可自行核对分发哈希，但不强制
要求归档存在。未来自动下载或正式发行可以锁定已发布归档的 SHA-256、上游提交、
运行时修订号和脚本提交，下载后核对并完整解压。公共 Release URL 直接下载，
消费端无需 GitHub token 或 Actions 权限。

COLMAP 4.2.1 使用 `FeatureExtraction.use_gpu`、`FeatureMatching.use_gpu` 和 Ceres
CPU 参数；已有调用方应确认自身兼容的 CLI 家族和能力检测。
CPU fallback 只针对 COLMAP。CUDA SIFT 与真实 Caspar GPU BA 仍标注“未验证”，
不承诺 OOOSplat/Brush 或其他项目的训练功能无需 GPU。
