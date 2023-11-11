from os import PathLike


def pad(path: PathLike, size: int):
    padding = b'\0' * 8192
    with open(path, mode='ab') as file:
        while file.tell() < size:
            n = min(size - file.tell(), len(padding))
            file.write(padding[0:n])
