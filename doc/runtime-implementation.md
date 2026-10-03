<!-- SPDX-License-Identifier: BSD-3-Clause -->
# 实施交付记录（2026-10-04）

工作目录为 `E:\Project\ooosplat-colmap`，origin 为
`https://github.com/ooolabdev/ooosplat-colmap.git`。
克隆后初始工作区干净，fork HEAD 为 `26600f8f660ef8abfa0872c078ed917ab8e60d5c`。
已读取 AGENTS.md 并检查覆盖指令、现有工作流与工作区。
改动限于此 fork 的构建、测试、分发与相应文档；没有改动算法源码、Caspar
生成文件或 OOOSplat 仓库。未提交、推送、触发远程构建或发布 Release。

## 修改文件

已有文件：

- `.github/workflows/build-windows.yml`
- `.github/workflows/build-ubuntu.yml`
- `.github/workflows/build-mac.yml`
- `.github/workflows/build-docker.yml`
- `.github/workflows/build-pycolmap.yml`
- `.gitignore`
- `README.md`

新增文件：

- `.github/workflows/runtime-build.yml`
- `.github/workflows/runtime-release.yml`
- `.github/workflows/runtime-checks.yml`
- `scripts/runtime/toolchain-lock.json`
- `scripts/runtime/runtime.py`
- `scripts/runtime/setup-windows.ps1`
- `scripts/runtime/common.py`
- `scripts/runtime/bundle.py`
- `scripts/runtime/smoke.py`
- `scripts/runtime/validation.Dockerfile`
- `scripts/runtime/release.py`
- `scripts/runtime/ruff.toml`
- `scripts/runtime/VALIDATION-LICENSE.txt`
- `scripts/runtime/tests/test_runtime.py`
- `scripts/runtime/tests/fixtures/cuda-linux.json`
- `scripts/runtime/tests/fixtures/cuda-windows.json`
- `doc/runtime-distribution.md`
- `doc/runtime-ooosplat.md`
- `doc/runtime-implementation.md`

`.runtime/` 是忽略的本地验证工作目录，含静态检查工具和下载检查资料，不进入运行包。

## 已执行验证

- Python unittest：30 个测试通过。覆盖真实官方 CUDA JSON 的原始摘要、LF/CRLF、
  缺字段和混装、错误 nvcc/CMake 路径/版本/架构、实际命令缺失或禁用现代预处理器、
  超长源/对象/依赖/临时路径、缓存兼容和 workflow 失败保存合同、静态 ELF 排除、
  未知共享库保留及重定位来源复用、缺库/插件/许可证、蒙版失败、验收失败禁止交付、
  三平台 Release 身份一致性、外部及内部哈希、已有标签/Release 拒绝、
  必需离线词汇树的缺失/大小/摘要/FAISS 格式，以及非相邻闭环匹配和发布资源阻断。
- Ruff 0.14.10：新增 Python 脚本静态检查及格式检查通过。
- actionlint 1.7.12：三个新 workflow 静态检查通过；下载包已核对官方发行摘要。
  本地禁用了未安装的 shellcheck/pyflakes 集成；Python 由 Ruff 检查。
- PowerShell AST parser：`setup-windows.ps1` 语法检查通过。
- CLI 的 `--help` 可运行；Git diff 空白检查通过。
- 上游固定提交的版本、CMake 接口、Caspar 文件路径及依赖来源已只读核对。
  `src/`、`cmake/`、根 CMakeLists 与上游 vcpkg 文件没有工作区改动。
- 从官方 Release 下载了实际 SIFT/FAISS 词汇树到此 fork 的忽略目录，验证
  72,412,636 字节、官方 SHA-256 与 v1/128/64 头信息一致；来源和摘要还与
  锁定上游 `retrieval/resources.h` 对照。没有从 OOOSplat 拷贝或改写其资源。

Windows 沙箱临时目录 ACL 会阻止 tempfile 测试清理，因此最终测试在权限允许的
本地进程中运行，TEMP/TMP 限定为仓库内 `.runtime/test-tmp`；没有远程构建调用。
单元测试中的发布归档、ELF 和小型词汇树字节是阻断逻辑样本，
不是可用 COLMAP 二进制或可用 FAISS 索引。

## 尚未验证

三平台真实依赖安装、编译、原生预检、运行库收集、平台重定位/签名、图像解码、
CPU SIFT/匹配/重建/Ceres BA、重新解压验收，以及 Actions Cache 的实际远程恢复/
失败保存尚未执行。静态和单元测试不能证明这些流程已经成功。
离线词汇树在 COLMAP 4.2.1 内的真实加载和 CPU 闭环匹配尚未执行，
已加入三平台打包及解压后的必过验收项，失败时不上传/发布可用包。
Windows 10/11 桌面兼容、CUDA SIFT 和真实 Caspar GPU BA 均未实机验证。
Release 工作流只有本地阻断测试，未发生实际公开发布。

因此本次交付是可审阅的构建与分发实现，**不宣称三平台构建成功或已有可用运行包**。

## 首次手动执行

获得明确提交/推送/触发授权后，将这些文件提交并推送到此 fork，再在 Actions
手动运行 **COLMAP runtime build**，选择 `platform=all`。
三平台通过后，用同一 run ID/attempt 在 **COLMAP runtime release** 中
保持 `publish=false` 生成发布预览；公开发布需要额外明确确认后再选 `publish=true`。
完整的下载、复现、兼容与许可证说明见 [分发文档](runtime-distribution.md)，
OOOSplat 的开发接入边界见 [接入文档](runtime-ooosplat.md)。
