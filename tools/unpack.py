#!/usr/bin/env python3

from __future__ import annotations
import argparse
import gzip
import json
from os import PathLike
from pathlib import Path
from utils import cpiofile
from utils.android import unpack_android_bootimg
from utils.rockchip import unpack_rockchip_krnl_bootimg


def parse_cmdline():
    parser = argparse.ArgumentParser()
    parser.add_argument("--boot_img", type=str, required=True, help="boot.img file to unpack")
    parser.add_argument("--out", type=str, required=True, help="Output dir where files will be unpacked")
    return parser.parse_args()


def cpio_info_to_dict(ci: cpiofile.CpioInfo):
    return ci.name, {"uid": ci.uid, "gid": ci.gid, "mode": oct(ci.mode & 0o777)[2:], "mtime": ci.mtime}


def unpack(boot_img: PathLike, out_dir: PathLike):
    try:
        info = unpack_android_bootimg(boot_img, out_dir)
    except ValueError:
        # Not an android boot image.
        info = unpack_rockchip_krnl_bootimg(boot_img, out_dir)

    info["image_size"] = Path(boot_img).stat().st_size

    ramdisk_extracted = Path(out_dir, "ramdisk.extracted")
    ramdisk_extracted.mkdir(parents=True, exist_ok=True)
    ramdisk = Path(out_dir, "ramdisk")

    with cpiofile.open(ramdisk, mode="r") as f:
        info["ramdisk_compression"] = "gzip" if isinstance(f.fileobj, gzip.GzipFile) else "none"
        info["ramdisk_files"] = dict(map(cpio_info_to_dict, f.getmembers()))
        f.extractall(path=ramdisk_extracted)

    Path(out_dir, "info.json").write_text(json.dumps(info, indent=2))


def main():
    args = parse_cmdline()
    unpack(Path(args.boot_img).absolute().resolve(), Path(args.out).absolute().resolve())


if __name__ == '__main__':
    main()
