<!-- SPDX-License-Identifier: BSD-3-Clause -->
# COLMAP 4.2.1 预编译运行包

本 fork 维护构建、验证与分发流程。上游算法源码和 Caspar 生成文件保持原样。
当前新增流程尚未完成真实三平台编译验收，首次构建通过之前没有可用包承诺。
COLMAP 上游版本为 `4.2.1`；本仓库打包修订版本为 `runtime.1`，首版标签为
`colmap-4.2.1-runtime.1`。修订运行包时增加锁定文件中的 `runtimeRevision`，
上游版本与运行时修订号分别管理，禁止覆盖已有标签或资产。

## 下载与解压

发布后从 [公开 Releases](https://github.com/ooolabdev/ooosplat-colmap/releases)
选择平台对应的归档、相邻 `.sha256` 和 `.manifest.json`。
Release 页面提供带固定标签和资产名称的直接下载链接，下载者无需 token、
Actions 权限、CUDA Toolkit、vcpkg 或本地编译。
不要将 `diagnostics-*` 当作运行包。Actions Artifact 是维护者交付入口，
保留 30 天，访问条件由 GitHub 决定；公开下载请使用 Release。

Windows 用 PowerShell 校验后解压（将文件名替换为下载的实际名称）：

```powershell
$archive = 'colmap-4.2.1-runtime.1-windows-x64-<sha>-run<id>-attempt<attempt>.zip'
$expected = ((Get-Content -LiteralPath "$archive.sha256").Trim() -split '\s+')[0]
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
    throw 'SHA-256 mismatch'
}
Expand-Archive -LiteralPath $archive -DestinationPath .\colmap-runtime
& .\colmap-runtime\bin\colmap.exe -h
```

Linux：

```sh
sha256sum -c colmap-*.tar.xz.sha256
mkdir colmap-runtime
tar -xJf <实际归档名>.tar.xz -C colmap-runtime
./colmap-runtime/bin/colmap -h
```

macOS 将校验命令改为 `shasum -a 256 -c <实际归档名>.tar.xz.sha256`，
解压和运行命令与 Linux 相同。macOS 文件经过 ad-hoc 签名及签名验证，
没有 Developer ID 公证；浏览器下载可能触发 Gatekeeper，由使用者确认来源后
按 macOS 正常安全提示处理。

归档顶层直接包含：

```text
bin/colmap.exe           # Windows；Linux/macOS 为 bin/colmap
lib/                    # 平台运行库，含 lib/validation/model 小型验收模型
lib/colmap/vocab_tree_faiss_flickr100K_words256K.bin  # 必需离线 SIFT 词汇树
licenses/
BUILD-INFO.json
BUNDLED-COMPONENTS.json
SHA256SUMS
```

保留整个目录，移动时一起移动 `bin`、`lib` 和其余文件。
Windows DLL 位于 `bin`，无需设置开发库 PATH。Linux 使用相对 `$ORIGIN`，
macOS 使用相对 loader/rpath。内部 `SHA256SUMS` 覆盖所有运行文件，排除自身；
归档 SHA-256 和压缩字节数保存在外部清单，避免自引用。
SHA-256 可检测损坏；需锁定可信版本时，在消费项目自己的清单中记录预先审核的哈希。

## 能力与兼容范围

| 平台 | 构建 runner / 编译器 | 兼容目标 | 能力 |
|---|---|---|---|
| Windows x64 | windows-2022 / VS2022 MSVC | Windows 10 22H2、Windows 11 | CPU SIFT、CUDA SIFT、Ceres CPU BA、Caspar f32 |
| Linux x64 | ubuntu-22.04 / GCC 12 | Ubuntu 22.04、glibc 2.35 | CPU SIFT、CUDA SIFT、Ceres CPU BA、Caspar f32 |
| macOS arm64 | macos-14 / Apple Clang | macOS 14 Apple Silicon | CPU SIFT、Ceres CPU BA |

runner 架构以 [GitHub 官方说明](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
和运行时检查为准。实际镜像、编译器、Windows SDK / Xcode SDK 与依赖版本写入
`BUILD-INFO.json`。Windows Server runner 实测与 Windows 10/11 桌面兼容目标分别记录，
没有通过桌面实机测试便不声称桌面验证完成。Linux 对所有打包 ELF 检查 glibc 符号要求，
再在干净 Ubuntu 22.04 容器验收；macOS 检查全部 Mach-O 的 arm64 和部署目标。

保留数据库、特征匹配、稀疏重建、JPEG/PNG、独立灰度蒙版。八个上游开关
`GUI_ENABLED`、`OPENGL_ENABLED`、`MVS_ENABLED`、`ONNX_ENABLED`、`CGAL_ENABLED`、
`LSD_ENABLED`、`DOWNLOAD_ENABLED`、`TESTS_ENABLED` 全部关闭。
因此没有 GUI、稠密 MVS 或 ONNX 特征提取能力；工具仍保留 CUDA SIFT 所需的
GL/GLEW/X11 编译依赖及其实际运行闭包。

三平台都保留 FAISS 检索、词汇树匹配和 sequential 闭环检测所需模块，并打包
锁定源码 `retrieval/resources.h` 指定的官方 SIFT 词汇树：
`vocab_tree_faiss_flickr100K_words256K.bin`，未压缩大小 72,412,636 字节，
SHA-256 为 `96ca8ec8ea60b1f73465aaf2c401fd3b3ca75cdba2d3c50d6a2f6f760f275ddc`。
该文件来自 [COLMAP 官方 3.11.1 Release](https://github.com/colmap/colmap/releases/tag/3.11.1)，
采用 FAISS v1 / SIFT 128 维格式，锁定源码包含该格式的读取兼容分支，
保留上游版权/许可证说明。它是必需运行资源，不参与“未使用模型”排除；
其他未使用词汇树和 ONNX 模型仍不打包。

由于 `DOWNLOAD_ENABLED=OFF`，调用者须显式传入包内树路径；不依赖首次运行联网下载
或用户缓存。验证同时核对大小、SHA-256、FAISS 头信息、许可证和组件清单；
在未执行 exhaustive 匹配的独立数据库上运行 CPU sequential 闭环检测，要求至少
一对非相邻图像产生有效几何匹配。缺树、损坏、错误格式或离线加载/闭环匹配失败
都会阻止 Artifact 与 Release 交付。真实验收仍由首次三平台构建确认。

Windows/Linux 锁定 CUDA 13.2.0 和 GPU 架构 `75;80;86;89;90;100;120`。
驱动不随包分发，CPU 流程无需 GPU；GPU 功能需要支持相应 GPU 与 CUDA 的 NVIDIA 驱动。
[NVIDIA 发布说明](https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html#cuda-driver)
将 CUDA 13.2 对应到 R595 驱动分支；13.x 小版本兼容的基础范围为驱动 >=580，
但该范围有功能限制，不能作为本包最低实测驱动承诺。
[小版本兼容说明](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)
特别指出较新 PTX 不能依赖旧驱动的小版本兼容。优先采用明确支持 CUDA 13.2 和目标
GPU 的 R595 或后续驱动，具体版本随操作系统和 GPU 查询官方支持范围。

**CUDA SIFT 和真实 Caspar GPU BA：未验证。** CLI、CCCL 编译探测和 CPU 验收
均不能替代真实 GPU BA。`gpuValidation` 单独记录，macOS 为“不适用”。
COLMAP 的 CPU 回退不承诺 Brush、OOOSplat 或其他项目的训练也能无 GPU 运行。

## CPU 使用示例

以下为 Linux/macOS，Windows 将入口换成 `bin\colmap.exe`。先创建 `work/sparse`。

```sh
COLMAP="$PWD/colmap-runtime/bin/colmap"
mkdir -p work/sparse
"$COLMAP" feature_extractor --database_path work/database.db --image_path images \
  --FeatureExtraction.use_gpu 0
"$COLMAP" exhaustive_matcher --database_path work/database.db \
  --FeatureMatching.use_gpu 0
"$COLMAP" mapper --database_path work/database.db --image_path images \
  --output_path work/sparse --Mapper.ba_use_gpu 0 \
  --Mapper.ba_local_backend CERES --Mapper.ba_global_backend CERES
```

RGBA PNG 的 alpha 不自动充当 COLMAP 蒙版。先把 alpha 提取为灰度 PNG，
用 `--ImageReader.mask_path masks` 指定；例如 `images/photo.png` 对应
`masks/photo.png.png`，黑色排除、白色保留。工作流检查被屏蔽区域的关键点，
不会把 RGBA 解码成功记作蒙版验收。

对有顺序的图像，下面的离线闭环匹配可以替换上面的 `exhaustive_matcher` 步骤：

```sh
VOCAB="$PWD/colmap-runtime/lib/colmap/vocab_tree_faiss_flickr100K_words256K.bin"
"$COLMAP" sequential_matcher --database_path work/database.db \
  --FeatureMatching.use_gpu 0 --SequentialMatching.loop_detection 1 \
  --SequentialMatching.vocab_tree_path "$VOCAB"
```

无序图像可使用 `vocab_tree_matcher --VocabTreeMatching.vocab_tree_path "$VOCAB"`
并指定数据库和 CPU 匹配参数。Windows 同样使用包内实际绝对路径。

## 来源、许可证和构建信息

`schemaVersion: 1` 的元数据分别记录：

- `sourceCommit`：上游 `bd1fcf654d2dd8fefa1466999c190a246f83f4b9`，源码版本 4.2.1。
- `scriptCommit`：工作流 fork 的实际构建提交，完整 SHA；不会冒充上游提交。
- 工具链、依赖版本、CUDA 发布版本和实际组件版本、架构、开关、实际 CMake 配置。
- 系统兼容目标和实测系统、CPU 验收报告、GPU 验收状态、资源预算和路径审计。
- 精简前 install 树和运行依赖闭包的字节数（定义写入 `bytes.definition`）。
  运行包补齐依赖和许可证后可能大于 install 树，不把两者比值伪称算法精简率。

`BUNDLED-COMPONENTS.json` 将组件关联到文件、版本、来源及 `licenses/` 文本，
同时列出系统基础库和外部驱动。静态链接组件也保留许可证。
缺少来源或许可证、缺库、插件缺失、路径重定位失败都会阻止交付。
仅排除明确开发类别（头文件、静态开发库、调试符号、测试程序、缓存、文档、示例、
未使用模型/词汇树）；保留未知用途运行库。运行包不含 NVIDIA 驱动、Toolkit stubs、
Brush、FFmpeg 可执行文件或 OOOSplat。

## 维护者首次执行

提交、推送、远程触发和发布都需要维护者明确授权，本次实施没有执行这些操作。
授权提交并推送后：

1. 在此 fork 启用 Actions，进入 **COLMAP runtime build**，手动选择 `platform=all`。
2. 三平台独立执行，`fail-fast=false`。检查各阶段日志以及可用包 Artifact，保留 30 天。
   失败只上传 `diagnostics-*`，不交付可用包；打包后重新解压并再次验收。
3. 记录同一成功 run ID、attempt 和脚本提交。进入 **COLMAP runtime release**，
   输入该 run、attempt、修订号，保持 `publish=false`，得到分发索引与发行说明预览。
4. 审核三平台原始归档及 GPU 未验证说明，获得单独发布授权后再以 `publish=true` 触发。
   发布复用原始归档，既不编译也不重打包。三平台身份、修订号或哈希不一致即拒绝。

Release 有独立并发锁。发布先原子创建新标签，再创建带原始资产的 draft，全部上传后
才公开。已有标签、Release 或资产不覆盖；上传失败可能留下 draft/标签，保留供人工
调查，不自动补写旧版本。处理残留需要另行授权；也可增加修订号后重新完整构建。
在 Artifact 30 天有效期内完成发布，过期需重新构建完整三平台。

原有继承的 Windows/Linux/macOS/Docker/pycolmap 完整构建入口改为手动；
普通 push/PR 只运行轻量 runtime 脚本测试及 Python 静态检查。

## 从源码复现

获取 Release 标注的 **scriptCommit**，检出此 fork 的该提交。脚本自动在独立短目录
检出并校验固定上游源码；不编译 fork HEAD。`scripts/runtime/toolchain-lock.json`
锁定 CMake 4.3.2、Ninja 1.12.1、ccache 4.13.6、vcpkg
`127402f1c75bb3d5ff6bce04b285faa4930a5aca`、CUDA 13.2.0 的官方 URL 和 SHA-256。
CUDA 从官方 redistributable 组件组装为开发工具链，保留官方 SDK 元数据原文、
每个组件归档哈希和文件摘要；不会伪称完整安装器安装。
Linux SDK 标识为 `13.2.20260303`，Windows 为 `13.2.0`；两者 nvcc/cudart/crt
为 `13.2.51`、cuRAND 为 `10.4.2.51`，实际 nvcc 为 `V13.2.51`。

在相应 runner 系统或同等开发系统上安装 Git、Python 3.12，
`python -m pip install Pillow==11.3.0 psutil==7.0.0`。
依赖准备命令见 `runtime-build.yml`；Windows 先在 VS2022 x64 开发环境运行，
Linux 准备 GCC/G++ 12 和工作流中的 GL/X11 开发依赖、Docker、patchelf；
macOS 准备 Xcode 与 arm64 Homebrew。

```sh
python scripts/runtime/runtime.py prepare --platform linux
python scripts/runtime/runtime.py preflight --platform linux
python scripts/runtime/runtime.py dependencies --platform linux
python scripts/runtime/runtime.py configure --platform linux
python scripts/runtime/runtime.py compile --platform linux
python scripts/runtime/runtime.py collect --platform linux
python scripts/runtime/runtime.py verify --platform linux
python scripts/runtime/runtime.py package --platform linux
python scripts/runtime/runtime.py stats --platform linux
```

Windows/macOS 替换 `--platform`。默认开发根目录 Windows `D:/c`、Linux/macOS
`/tmp/c`，源码 `s`、构建 `b`、临时 `t`、vcpkg `v`、依赖 `i`。
必须使用空的新根目录；不自动清除已有内容。
Windows 完整路径预算 <240 字符，TEMP <32 字符，预检在耗时依赖之前编译真实最长
Caspar f32 源文件及 CCCL，使用全部架构和 `/Zc:preprocessor`。
配置后还核对 Ninja 展开的实际命令、对象和依赖路径，不依赖注册表长路径设置。

macOS 隔离验收会临时隐藏 `/opt/homebrew`，仅允许在 `GITHUB_ACTIONS=true` 的
一次性 runner 上执行完整 `verify/package`，结束后恢复。不要在个人 macOS 工作站上
伪造该标志；在那里可构建和 collect，完整隔离验收交由一次性 Actions runner。

Homebrew 保留现有方案，记录实际公式版本、来源校验和配方身份，未将公式版本
改为另一套全锁定依赖管理。runner 镜像和系统编译器也会更新，身份变化会隔离缓存；
此处“复现”指锁定算法、工具和配置后重跑验收，不保证任意日期逐字节一致。
需要历史完全相同 Homebrew 依赖时，在一次性环境按包元数据恢复相应配方和 bottle。

vcpkg 二进制缓存与 ccache 在兼容前缀内恢复。键包含平台、架构、实际编译器/SDK、
CUDA、vcpkg 提交、triplet、依赖配置、构建配置；编译缓存进一步包含实际安装依赖。
单纯 fork 提交变化不清空全部依赖缓存。每次保存用 run/attempt 新键，失败也保留
成功缓存条目；空缓存执行同样检查。ccache 先通过重复实际编译证明命中，正常构建
记录 hit/miss/uncacheable。并行度按 `min(2, CPU, 可用内存/3 GiB)`（至少 1）
限制 vcpkg 和 COLMAP，日志记录每个命令耗时及可用内存预算。

本地脚本检查：

```sh
python -m pip install PyYAML==6.0.2 Pillow==11.3.0 psutil==7.0.0 ruff==0.14.10
python -m ruff check scripts/runtime
python -m ruff format --check scripts/runtime
python -m unittest discover -s scripts/runtime/tests -v
```

测试的 CUDA JSON 来自锁定文件列出的 NVIDIA 官方元数据，包含原始下载摘要，
测试覆盖 LF/CRLF、缺字段、混装、编译器和路径配置错误及分发阻断。
这些本地脚本测试不等于三平台构建或真实 GPU 验收。
