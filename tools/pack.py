#!/usr/bin/env python3

from __future__ import annotations
import argparse
import json
from os import PathLike
from pathlib import Path
from utils import cpiofile
from utils.android import pack_android_bootimg
from utils.rockchip import pack_rockchip_krnl_bootimg


def parse_cmd_line():
    parser = argparse.ArgumentParser()
    parser.add_argument("--boot_img", type=str, required=True, help="boot.img file to write to")
    parser.add_argument("--work_dir", type=str, required=True, help="dir where files have previously been unpacked")
    return parser.parse_args()


def pack(boot_img: PathLike, work_dir: PathLike, extra_ramdisk_files=None):
    info = json.loads(Path(work_dir, "info.json").read_text(encoding="utf8"))
    boot_magic = info["boot_magic"]

    if boot_magic not in ["KRNL", "ANDROID!"]:
        raise ValueError(f"Unsupported boot_magic: {boot_magic}")

    ramdisk_files = info["ramdisk_files"]
    if extra_ramdisk_files is not None:
        ramdisk_files = {**ramdisk_files, **extra_ramdisk_files}

    ramdisk_compression = info["ramdisk_compression"]
    open_mode = "w:gz" if ramdisk_compression == "gzip" else "w"

    ramdisk_extracted = Path(work_dir, "ramdisk.extracted.patched")
    if not ramdisk_extracted.exists():
        ramdisk_extracted = Path(work_dir, "ramdisk.extracted")
    ramdisk_patched = Path(work_dir, "ramdisk.patched")

    default_file_attr = {"uid": 0, "gid": 0, "mode": "644", "mtime": 0}
    default_dir_attr = {"uid": 0, "gid": 0, "mode": "755", "mtime": 0}

    with cpiofile.open(ramdisk_patched, mode=open_mode) as f:
        files = list(ramdisk_extracted.glob("**/*"))
        for file in files:
            arcname = str(Path(file).relative_to(ramdisk_extracted))
            cpioinfo = f.getcpioinfo(file, arcname)
            file_attr = ramdisk_files.get(arcname, default_dir_attr if cpioinfo.isdir() else default_file_attr)
            cpioinfo.uid = file_attr["uid"]
            cpioinfo.gid = file_attr["gid"]
            cpioinfo.mode = int(file_attr["mode"], 8) | (cpioinfo.mode & 0o177000)
            cpioinfo.mtime = file_attr["mtime"]
            if cpioinfo.isreg():
                with open(file, "rb") as fileobj:
                    f.addfile(cpioinfo, fileobj)
            else:
                f.addfile(cpioinfo)

    if boot_magic == "KRNL":
        pack_rockchip_krnl_bootimg(boot_img, info)
    elif boot_magic == "ANDROID!":
        pack_android_bootimg(boot_img, info)


def main():
    args = parse_cmd_line()
    pack(Path(args.boot_img).absolute().resolve(), Path(args.work_dir).absolute().resolve())


if __name__ == '__main__':
    main()
