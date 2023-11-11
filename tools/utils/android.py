from __future__ import annotations
import os
from mkbootimg.unpack_bootimg import unpack_bootimg, BootImageInfoFormatter
from mkbootimg.mkbootimg import main as mkbootimg_main
from pathlib import Path
from unittest.mock import patch
from . import pad


def unpack_android_bootimg(boot_img: os.PathLike, output_dir: os.PathLike):
    return unpack_bootimg(str(boot_img), str(output_dir)).__dict__


def pack_android_bootimg(boot_img: os.PathLike, info: dict):
    formatter = BootImageInfoFormatter()
    formatter.__dict__ = info
    cmd_args = formatter.format_mkbootimg_argument()
    cmd_args.extend(["--output", str(Path(boot_img).absolute().resolve())])
    image_dir = info["image_dir"]
    ramdisk = Path(image_dir, "ramdisk.patched")
    if not ramdisk.exists():
        ramdisk = Path(image_dir, "ramdisk")
    idx = cmd_args.index("--ramdisk")
    cmd_args[idx + 1] = str(ramdisk.absolute().resolve())

    with patch("sys.argv", ["mkbootimg.py"] + cmd_args):
        mkbootimg_main()

    image_size = info["image_size"]
    pad(boot_img, image_size)
