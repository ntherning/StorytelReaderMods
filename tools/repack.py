#!/usr/bin/env python3

from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
from os import PathLike
from pathlib import Path
from pack import pack
from unpack import unpack
from utils import patch_ng
import tempfile


def parse_cmd_line():
    parser = argparse.ArgumentParser()
    parser.add_argument("--boot_img", type=str, required=True, help="boot.img file to modify")
    parser.add_argument("--out_img", type=str, required=True, help="file to write the modified image to")
    parser.add_argument("--mods_dir", type=str, required=True,
                        help="directory containing patches and files to be copied/applied to the modified image")
    parser.add_argument("--work_dir", type=str, required=False,
                        help="directory where intermediary files will be written")
    return parser.parse_args()


def repack(boot_img: PathLike, out_img: PathLike, mods_dir: PathLike, work_dir: PathLike = None):
    if work_dir is None:
        work_dir = tempfile.mkdtemp()
    work_dir = Path(work_dir).absolute().resolve()
    print(f"Using work dir: {work_dir}")

    mods_dir = Path(mods_dir).absolute().resolve()

    patch_files = sorted(mods_dir.glob("*.patch"))
    extra_files_dir = mods_dir.joinpath("files")
    files_to_copy = list(filter(lambda f: Path(f).is_file(), extra_files_dir.glob("**/*")))

    if os.environ.get("DEBUG") is not None:
        patch_ng.setdebug()

    print(f"Unpacking {boot_img}")
    unpack(boot_img, work_dir)

    for patch_file in patch_files:
        print(f"Applying patch file: {patch_file}")
        p = patch_ng.fromfile(patch_file)
        if not p.apply(strip=1, root=work_dir.joinpath("ramdisk.extracted"), fuzz=True):
            print("Patch failed")
            sys.exit(1)

    for in_file in files_to_copy:
        in_file = Path(in_file)
        out_file = work_dir.joinpath("ramdisk.extracted").joinpath(in_file.relative_to(extra_files_dir))

        out_file.parent.mkdir(parents=True, exist_ok=True)

        print(f"Copying {in_file} to {out_file}")
        shutil.copyfile(in_file, out_file, follow_symlinks=False)
        shutil.copymode(in_file, out_file, follow_symlinks=False)

    extra_ramdisk_files_json = mods_dir.joinpath("extra_ramdisk_files.json")
    extra_ramdisk_files_attrs = json.loads(extra_ramdisk_files_json.read_text())\
        if extra_ramdisk_files_json.exists() else None

    print(f"Packing {out_img}")
    pack(out_img, work_dir, extra_ramdisk_files_attrs)


def main():
    args = parse_cmd_line()
    repack(
        Path(args.boot_img).absolute().resolve(),
        Path(args.out_img).absolute().resolve(),
        Path(args.mods_dir).absolute().resolve(),
        args.work_dir
    )


if __name__ == '__main__':
    main()
