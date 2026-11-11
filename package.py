#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按版本号把 WoW 插件打包到 dist/。

每个插件从自己的 <Name>.toc 里读 `## Version:` 作为版本号，
打出的包命名为 dist/<Name>-<version>.zip，内部顶层目录就是插件目录名，
解压到 Interface/AddOns 下即可直接用。

如果 dist/<Name>-<version>.zip 已经存在，先删掉再重新打（重打同版本）。

用法：
    python package.py            # 打全部已知插件
    python package.py GoldQuestLedger GoldDailyLedger   # 只打指定的
"""
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")

# 插件目录名 = .toc 文件名 = AddOns 里的目录名
# GoldLedger 不是本仓库的插件（第三方参考副本），不纳入打包。
PLUGINS = ["AHSnipe", "GoldDailyLedger", "GoldQuestLedger"]

# 不进包的东西：版本控制目录、缓存、临时/备份文件
SKIP_DIRS = {".git", "__pycache__", ".svn"}
SKIP_FILE_SUFFIX = (".bak", ".tmp", ".orig", "~")

VERSION_RE = re.compile(r"^\s*##\s*Version:\s*(.+?)\s*$", re.MULTILINE)
# Windows/zip 文件名里不允许的字符（版本号里的 + . - _ 都合法，只挡这几样）
BAD_CHAR_RE = re.compile(r'[\\/:*?"<>|]')


def read_version(toc_path):
    with open(toc_path, encoding="utf-8") as f:
        text = f.read()
    m = VERSION_RE.search(text)
    if not m:
        raise SystemExit("读不到版本号: " + toc_path)
    return m.group(1).strip()


def safe_version(version):
    return BAD_CHAR_RE.sub("_", version)


def package(name):
    src = os.path.join(ROOT, name)
    toc = os.path.join(src, name + ".toc")
    if not os.path.isdir(src):
        print("跳过（目录不存在）: " + name)
        return False
    if not os.path.isfile(toc):
        print("跳过（缺 .toc）: " + name)
        return False

    version = read_version(toc)
    zip_name = name + "-" + safe_version(version) + ".zip"
    zip_path = os.path.join(DIST, zip_name)

    if os.path.exists(zip_path):
        os.remove(zip_path)
        print("已删除旧包: " + zip_name)

    os.makedirs(DIST, exist_ok=True)

    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.lower().endswith(SKIP_FILE_SUFFIX):
                    continue
                full = os.path.join(dirpath, fn)
                arc = os.path.join(name, os.path.relpath(full, src))
                z.write(full, arc)
                count += 1

    size = os.path.getsize(zip_path)
    print("已打包: %s  (%d 个文件, %d 字节, 版本 %s)"
          % (zip_name, count, size, version))
    return True


def main():
    targets = sys.argv[1:] or PLUGINS
    done = 0
    for t in targets:
        if t not in PLUGINS:
            print("未知插件: %s（已知: %s）" % (t, ", ".join(PLUGINS)))
            continue
        if package(t):
            done += 1
    print("\n完成：%d 个插件打包到 %s" % (done, DIST))


if __name__ == "__main__":
    main()
