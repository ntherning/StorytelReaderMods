from __future__ import annotations
import os
import shutil
from pathlib import Path
from struct import pack, unpack
from . import pad


def unpack_rockchip_krnl_bootimg(boot_img: os.PathLike, output_dir: os.PathLike):
    def check_krnl_initrd_img(image_file_path: os.PathLike):
        with open(image_file_path, 'rb') as f:
            magic = unpack('4s', f.read(4))[0].decode()
            if magic == 'KRNL':
                f.seek(8)
                gzip_magic = unpack('2s', f.read(2))[0]
                if gzip_magic == b'\x1f\x8b':
                    return
        raise ValueError(f'Not a KRNL initrd image, magic: {magic}')

    check_krnl_initrd_img(boot_img)
    with open(boot_img, 'rb') as image_file:
        image_file.seek(4)
        size = unpack('<I', image_file.read(4))[0]
        image_file.seek(8)
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(output_dir, 'ramdisk'), 'wb') as file_out:
            file_out.write(image_file.read(size))

    return {"boot_magic": "KRNL", "image_dir": str(output_dir)}


def pack_rockchip_krnl_bootimg(boot_img: os.PathLike, info: dict):
    image_dir = Path(info["image_dir"])
    ramdisk = Path(image_dir, "ramdisk.patched")
    if not ramdisk.exists():
        ramdisk = Path(image_dir, "ramdisk")
    with open(boot_img, mode='wb') as image_file:
        image_file.write(b'KRNL')
        image_file.write(pack('<I', ramdisk.stat().st_size))
        crc = rkcrc32(ramdisk)
        with open(ramdisk, mode='rb') as ramdisk_file:
            shutil.copyfileobj(ramdisk_file, image_file)
        image_file.write(pack('<I', crc))

    image_size = info["image_size"]
    pad(boot_img, image_size)


# static inline uint32_t
# rkcrc32(uint32_t crc, uint8_t *buf, uint64_t size)
# {
# 	int i;
#
# 	while (size-- > 0) {
# 		crc ^= *buf++ << 24;
# 		for (i = 0; i < 8; i++) {
# 			if (crc & 0x80000000)
# 				crc = (crc << 1) ^ 0x04c10db7;
# 			else
# 				crc = (crc << 1);
# 		}
# 	}
#
# 	return crc;
# }
def rkcrc32(p: str | os.PathLike[str]):
    with open(p, mode='rb') as f:
        crc = 0
        while True:
            data = f.read(8192)
            if len(data) == 0:
                return crc
            for b in data:
                crc ^= ((b << 24) & 0xffffffff)
                for i in range(0, 8):
                    if crc & 0x80000000:
                        crc = ((crc << 1) & 0xffffffff) ^ 0x04c10db7
                    else:
                        crc = ((crc << 1) & 0xffffffff)
