# SPDX-License-Identifier: BSD-3-Clause
FROM ubuntu:22.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 python3-pil python3-psutil libstdc++6 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# No CUDA Toolkit, vcpkg, COLMAP, image libraries, GL/X11 dev packages or GPU.
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 CUDA_VISIBLE_DEVICES=-1
