# -*- coding: utf-8 -*-
#-------------------------------------------------------------------
# cpiofile.py
#-------------------------------------------------------------------

# Copyright (C) 2002 Lars Gustäbel <lars@gustaebel.de>
# Copyright (c) 2013, Citrix Inc.
# All rights reserved.
#
# Permission  is  hereby granted,  free  of charge,  to  any person
# obtaining a  copy of  this software  and associated documentation
# files  (the  "Software"),  to   deal  in  the  Software   without
# restriction,  including  without limitation  the  rights to  use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies  of  the  Software,  and to  permit  persons  to  whom the
# Software  is  furnished  to  do  so,  subject  to  the  following
# conditions:
#
# The above copyright  notice and this  permission notice shall  be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS  IS", WITHOUT WARRANTY OF ANY  KIND,
# EXPRESS OR IMPLIED, INCLUDING  BUT NOT LIMITED TO  THE WARRANTIES
# OF  MERCHANTABILITY,  FITNESS   FOR  A  PARTICULAR   PURPOSE  AND
# NONINFRINGEMENT.  IN  NO  EVENT SHALL  THE  AUTHORS  OR COPYRIGHT
# HOLDERS  BE LIABLE  FOR ANY  CLAIM, DAMAGES  OR OTHER  LIABILITY,
# WHETHER  IN AN  ACTION OF  CONTRACT, TORT  OR OTHERWISE,  ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.

"""Read from and write to cpio format archives.

   Of course: Only platforms with filesystems supporting hardlinks
   support extracting cpio files containing hardlinks, but Dom0 does.

   Derived from Lars Gustäbel's tarfile.py
"""
from __future__ import print_function
# pyre is not as good as other static analysis tools in inferring the correct types:
# pyre-ignore-all-errors[6,16]

__version__ = "0.1"
__author__  = "Simon Rowe"
__credits__ = "Lars Gustäbel, Gustavo Niemeyer, Niels Gustäbel, Richard Townsend."

#---------
# Imports
#---------
import bz2
import gzip
import sys
import os
import shutil
import stat
import errno
import time
import struct
import copy
import io
from typing import IO, TYPE_CHECKING, Any, List, Optional, cast

from utils import six

if TYPE_CHECKING:
    from gzip import GzipFile
    from typing_extensions import Literal

if sys.platform == 'mac':
    # This module needs work for MacOS9, especially in the area of pathname
    # handling. In many places it is assumed a simple substitution of / by the
    # local os.path.sep is good enough to convert pathnames, but this does not
    # work with the mac rooted:path:name versus :nonrooted:path:name syntax
    raise ImportError("cpiofile does not work for platform==mac")

try:
    import grp as GRP, pwd as PWD
except ImportError:
    GRP = PWD = None  # type: ignore[assignment] # pragma: no cover

# pylint: skip-file
# from cpiofile import *
__all__ = ["CpioFile", "CpioInfo", "is_cpiofile", "CpioError"]

#---------------------------------------------------------
# cpio constants
#---------------------------------------------------------
MAGIC_NEWC      = 0x070701           # magic for SVR4 portable format (no CRC)
TRAILER_NAME    = b"TRAILER!!!"      # filename in final member
WORDSIZE        = 4                  # pad size
NUL             = b"\0"              # the null character
BLOCKSIZE       = 512                # length of processing blocks
HEADERSIZE_SVR4 = 110                # length of fixed header

#---------------------------------------------------------
# Bits used in the mode field, values in octal.
#---------------------------------------------------------
S_IFLNK = 0o120000        # symbolic link
S_IFREG = 0o100000        # regular file
S_IFBLK = 0o060000        # block device
S_IFDIR = 0o040000        # directory
S_IFCHR = 0o020000        # character device
S_IFIFO = 0o010000        # fifo

TSUID   = 0o4000          # set UID on execution
TSGID   = 0o2000          # set GID on execution
TSVTX   = 0o1000          # reserved

TUREAD  = 0o400           # read by owner
TUWRITE = 0o200           # write by owner
TUEXEC  = 0o100           # execute/search by owner
TGREAD  = 0o040           # read by group
TGWRITE = 0o020           # write by group
TGEXEC  = 0o010           # execute/search by group
TOREAD  = 0o004           # read by other
TOWRITE = 0o002           # write by other
TOEXEC  = 0o001           # execute/search by other

#---------------------------------------------------------
# Some useful functions
#---------------------------------------------------------

def copyfileobj(src, dst, length=None):
    """Copy length bytes from fileobj src to fileobj dst.
       If length is None, copy the entire content.
    """
    if length == 0:
        return
    if length is None:
        shutil.copyfileobj(src, dst)
        return

    bufsize = 16 * 1024
    blocks, remainder = divmod(length, bufsize)
    for b in range(blocks):
        buf = src.read(bufsize)
        if len(buf) < bufsize:
            raise IOError("end of file reached")
        dst.write(buf)

    if remainder != 0:
        buf = src.read(remainder)
        if len(buf) < remainder:
            raise IOError("end of file reached")
        dst.write(buf)
    return

FILEMODE_TABLE = (
    ((S_IFLNK,      "l"),
     (S_IFREG,      "-"),
     (S_IFBLK,      "b"),
     (S_IFDIR,      "d"),
     (S_IFCHR,      "c"),
     (S_IFIFO,      "p")),

    ((TUREAD,       "r"),),
    ((TUWRITE,      "w"),),
    ((TUEXEC|TSUID, "s"),
     (TSUID,        "S"),
     (TUEXEC,       "x")),

    ((TGREAD,       "r"),),
    ((TGWRITE,      "w"),),
    ((TGEXEC|TSGID, "s"),
     (TSGID,        "S"),
     (TGEXEC,       "x")),

    ((TOREAD,       "r"),),
    ((TOWRITE,      "w"),),
    ((TOEXEC|TSVTX, "t"),
     (TSVTX,        "T"),
     (TOEXEC,       "x"))
)

def filemode(mode):
    """Convert a file's mode to a string of the form
       -rwxrwxrwx.
       Used by CpioFile.list()
    """
    perm = []
    for table in FILEMODE_TABLE:
        for bit, char in table:
            if mode & bit == bit:
                perm.append(char)
                break
        else:
            perm.append("-")
    return "".join(perm)


def normpath(path):
    if os.sep != "/":
        return os.path.normpath(path).replace(os.sep, "/")
    else:
        return os.path.normpath(path)


class CpioError(Exception):
    """Base exception."""
    pass
class ExtractError(CpioError):
    """General exception for extract errors."""
    pass
class ReadError(CpioError):
    """Exception for unreadble cpio archives."""
    pass
class CompressionError(CpioError):
    """Exception for unavailable compression methods."""
    pass
class StreamError(CpioError):
    """Exception for unsupported operations on stream-like CpioFiles."""
    pass

#---------------------------
# internal stream interface
#---------------------------
class _LowLevelFile(object):
    """Low-level file object. Supports reading and writing.
       It is used instead of a regular file object for streaming
       access.
    """

    def __init__(self, name, mode):
        mode = {
            "r": os.O_RDONLY,
            "w": os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        }[mode]
        if not hasattr(os, "O_BINARY"):
            os.O_BINARY = 0
        mode |= os.O_BINARY
        self.fd = os.open(name, mode)

    def close(self):
        os.close(self.fd)

    def read(self, size):
        return os.read(self.fd, size)

    def write(self, s):
        os.write(self.fd, s)

class _Stream(object):
    """Class that serves as an adapter between CpioFile and
       a stream-like object.  The stream-like object only
       needs to have a read() or write() method and is accessed
       blockwise.  Use of gzip or bzip2 compression is possible.
       A stream-like object could be for example: sys.stdin,
       sys.stdout, a socket, a tape device etc.

       _Stream is intended to be used only internally.
    """

    def __init__(self, name, mode, comptype, fileobj, bufsize):
        """Construct a _Stream object.
        """
        self._extfileobj = True
        if fileobj is None:
            fileobj = _LowLevelFile(name, mode)
            self._extfileobj = False

        if comptype == '*':
            # Enable transparent compression detection for the
            # stream interface
            fileobj = _StreamProxy(fileobj)
            comptype = fileobj.getcomptype()

        self.name     = name or ""
        self.mode     = mode
        self.comptype = comptype
        self.fileobj  = fileobj
        self.bufsize  = bufsize
        self.buf      = b""
        self.pos      = 0
        self.closed   = False

        if comptype == "gz":
            try:
                import zlib
            except ImportError:
                raise CompressionError("zlib module is not available")
            self.zlib = zlib
            self.crc = zlib.crc32(b"")
            if mode == "r":
                self._init_read_gz()
            else:
                self._init_write_gz()

        if comptype == "bz2":
            if mode == "r":
                self.dbuf = b""
                self.cmp = bz2.BZ2Decompressor()
            else:
                self.cmp = bz2.BZ2Compressor()

        if comptype == "xz":
            try:
                import lzma
            except ImportError:
                raise CompressionError("lzma module is not available")
            if mode == "r":
                self.dbuf = b""
                self.cmp = lzma.LZMADecompressor()
            else:
                self.cmp = lzma.LZMACompressor()


    def __del__(self):
        if hasattr(self, "closed") and not self.closed:
            self.close()

    def _init_write_gz(self):
        """Initialize for writing with gzip compression.
        """
        self.cmp = self.zlib.compressobj(9, self.zlib.DEFLATED,
                                            -self.zlib.MAX_WBITS,
                                            self.zlib.DEF_MEM_LEVEL,
                                            0)
        timestamp = struct.pack("<L", int(time.time()))
        self.__write(b"\037\213\010\010%s\002\377" % timestamp)
        if self.name.endswith(".gz"):
            self.name = self.name[:-3]
        self.__write(six.ensure_binary(self.name) + NUL)

    def write(self, s):
        """Write string s to the stream.
        """
        if self.comptype == "gz":
            self.crc = self.zlib.crc32(s, self.crc)
        self.pos += len(s)
        if self.comptype != "cpio":
            s = cast(bz2.BZ2Compressor, self.cmp).compress(s)
        self.__write(s)

    def __write(self, s):
        """Write string s to the stream if a whole new block
           is ready to be written.
        """
        self.buf += s
        while len(self.buf) > self.bufsize:
            self.fileobj.write(self.buf[:self.bufsize])
            self.buf = self.buf[self.bufsize:]

    def close(self):
        """Close the _Stream object. No operation should be
           done on it afterwards.
        """
        if self.closed:
            return

        if self.mode == "w" and self.comptype != "cpio":
            self.buf += cast(bz2.BZ2Compressor, self.cmp).flush()

        if self.mode == "w" and self.buf:
            self.fileobj.write(self.buf)
            self.buf = b""
            if self.comptype == "gz":
                # The native zlib crc is an unsigned 32-bit integer, but
                # the Python wrapper implicitly casts that to a signed C
                # long.  So, on a 32-bit box self.crc may "look negative",
                # while the same crc on a 64-bit box may "look positive".
                # To avoid irksome warnings from the `struct` module, force
                # it to look positive on all boxes.
                self.fileobj.write(struct.pack("<L", self.crc & 0xffffffff))
                self.fileobj.write(struct.pack("<L", self.pos & 0xffffFFFF))

        if not self._extfileobj:
            self.fileobj.close()

        self.closed = True

    def _init_read_gz(self):
        """Initialize for reading a gzip compressed fileobj.
        """
        self.cmp = self.zlib.decompressobj(-self.zlib.MAX_WBITS)
        self.dbuf = b""

        # taken from gzip.GzipFile with some alterations
        if self.__read(2) != b"\037\213":
            raise ReadError("not a gzip file")
        if self.__read(1) != b"\010":
            raise CompressionError("unsupported compression method")

        flag = ord(self.__read(1))
        self.__read(6)

        if flag & 4:
            xlen = ord(self.__read(1)) + 256 * ord(self.__read(1))
            self.read(xlen)
        if flag & 8:
            while True:
                s = self.__read(1)
                if not s or s == NUL:
                    break
        if flag & 16:
            while True:
                s = self.__read(1)
                if not s or s == NUL:
                    break
        if flag & 2:
            self.__read(2)

    def tell(self):
        """Return the stream's file pointer position.
        """
        return self.pos

    def seek(self, pos=0):
        """Set the stream's file pointer to pos. Negative seeking
           is forbidden.
        """
        if pos - self.pos >= 0:
            blocks, remainder = divmod(pos - self.pos, self.bufsize)
            for i in range(blocks):
                self.read(self.bufsize)
            self.read(remainder)
        else:
            raise StreamError("seeking backwards is not allowed")
        return self.pos

    def read(self, size=None):
        """Return the next size number of bytes from the stream.
           If size is not defined, return all bytes of the stream
           up to EOF.
        """
        if size is None:
            t = []
            while True:
                buf = self._read(self.bufsize)
                if not buf:
                    break
                t.append(buf)
            buf = b"".join(t)
        else:
            buf = self._read(size)
        self.pos += len(buf)
        return buf

    def _read(self, size):
        """Return size bytes from the stream.
        """
        if self.comptype == "cpio":
            return self.__read(size)

        c = len(self.dbuf)
        t = [self.dbuf]
        while c < size:
            buf = self.__read(self.bufsize)
            if not buf:
                break
            buf = cast(bz2.BZ2Decompressor, self.cmp).decompress(buf)
            t.append(buf)
            c += len(buf)
        t = b"".join(t)
        self.dbuf = t[size:]
        return t[:size]

    def __read(self, size):
        """Return size bytes from stream. If internal buffer is empty,
           read another block from the stream.
        """
        c = len(self.buf)
        t = [self.buf]
        while c < size:
            buf = self.fileobj.read(self.bufsize)
            if not buf:
                break
            t.append(buf)
            c += len(buf)
        t = b"".join(t)
        self.buf = t[size:]
        return t[:size]
# class _Stream

class _StreamProxy(object):
    """Small proxy class that enables transparent compression
       detection for the Stream interface (mode 'r|*').
    """

    def __init__(self, fileobj):
        self.fileobj = fileobj
        self.buf = self.fileobj.read(BLOCKSIZE)

    def read(self, size):
        #self.read = self.fileobj.read
        setattr(self, "read", self.fileobj.read)
        return self.buf

    def getcomptype(self):
        if self.buf.startswith(b"\037\213\010"):
            return "gz"
        if self.buf.startswith(b"BZh91"):
            return "bz2"
        if self.buf.startswith(b"\xfd7zXZ\0"):
            return "xz"
        return "cpio"

    def close(self):
        self.fileobj.close()
# class StreamProxy

class _CMPProxy(object):

    blocksize = 16 * 1024

    def __init__(self, fileobj, mode):
        self.fileobj = fileobj
        self.mode = mode
        self.cmpobj = None
        self.buf = b""
        self.pos = 0

    def read(self, size):
        b = [self.buf]
        x = len(self.buf)
        while x < size:
            try:
                raw = self.fileobj.read(self.blocksize)
                assert self.cmpobj
                data = self.cmpobj.decompress(raw)
                b.append(data)
            except EOFError:
                break
            x += len(data)
        self.buf = b"".join(b)

        buf = self.buf[:size]
        self.buf = self.buf[size:]
        self.pos += len(buf)
        return buf

    def seek(self, pos):
        if pos < self.pos:
            self.init()
        self.read(pos - self.pos)

    def init(self):
        # implemented by subclasses
        raise NotImplementedError()

    def tell(self):
        return self.pos

    def write(self, data):
        self.pos += len(data)
        assert self.cmpobj
        raw = self.cmpobj.compress(data)
        self.fileobj.write(raw)

    def close(self):
        if self.mode == "w":
            assert self.cmpobj
            raw = self.cmpobj.flush()
            self.fileobj.write(raw)
        if not isinstance(self.fileobj, io.BytesIO):  # BytesIO() would free the archive on close()
            self.fileobj.close()
# class _CMPProxy


class _BZ2Proxy(_CMPProxy):
    """Small proxy class that enables external file object
       support for "r:bz2" and "w:bz2" modes. This is actually
       a workaround for a limitation in bz2 module's BZ2File
       class which (unlike gzip.GzipFile) has no support for
       a file object argument.
    """

    def __init__(self, fileobj, mode):
        # type:(IO[Any], str) -> None
        _CMPProxy.__init__(self, fileobj, mode)
        self.init()

    def init(self):
        self.pos = 0
        if self.mode == "r":
            self.cmpobj = bz2.BZ2Decompressor()
            self.fileobj.seek(0)
            self.buf = b""
        else:
            self.cmpobj = bz2.BZ2Compressor()

# class _BZ2Proxy


#------------------------
# Extraction file object
#------------------------
class _FileInFile(object):
    """A thin wrapper around an existing file object that
       provides a part of its data as an individual file
       object.
    """

    def __init__(self, fileobj, offset, size, sparse=None):
        self.fileobj = fileobj
        self.offset = offset
        self.size = size
        self.sparse = sparse
        self.position = 0

    def tell(self):
        """Return the current file position.
        """
        return self.position

    def seek(self, position):
        """Seek to a position in the file.
        """
        self.position = position

    def read(self, size=None):
        """Read data from the file.
        """
        if size is None:
            size = self.size - self.position
        else:
            size = min(size, self.size - self.position)

        if self.sparse is None:
            return self.readnormal(size)
        else:
            return self.readsparse(size)

    def readnormal(self, size):
        """Read operation for regular files.
        """
        self.fileobj.seek(self.offset + self.position)
        self.position += size
        return self.fileobj.read(size)

    def readsparse(self, size):
        """Read operation for sparse files.
        """
        data = []
        while size > 0:
            buf = self.readsparsesection(size)
            if not buf:
                break
            size -= len(buf)
            data.append(buf)
        return b"".join(data)

    def readsparsesection(self, size):
        """Read a single section of a sparse file.
        """
        section = self.sparse.find(self.position)

        if section is None:
            return b""

        size = min(size, section.offset + section.size - self.position)

        # if isinstance(section, _data):
        #     realpos = section.realpos + self.position - section.offset
        #     self.fileobj.seek(self.offset + realpos)
        #     self.position += size
        #     return self.fileobj.read(size)
        # else:
        #     self.position += size
        #     return NUL * size
#class _FileInFile

class ExFileObject(object):
    """File-like object for reading an archive member.
       Is returned by CpioFile.extractfile().
    """
    blocksize = 1024

    def __init__(self, cpiofile, cpioinfo):
        self.fileobj = _FileInFile(cpiofile.fileobj,
                                   cpioinfo.offset_data,
                                   cpioinfo.size,
                                   getattr(cpioinfo, "sparse", None))
        self.name = cpioinfo.name
        self.mode = "r"
        self.closed = False
        self.size = cpioinfo.size

        self.position = 0
        self.buffer = b""

    def read(self, size=None):
        """Read at most size bytes from the file. If size is not
           present or None, read all data until EOF is reached.
        """
        if self.closed:
            raise ValueError("I/O operation on closed file")

        buf = b""
        if self.buffer:
            if size is None:
                buf = self.buffer
                self.buffer = b""
            else:
                buf = self.buffer[:size]
                self.buffer = self.buffer[size:]

        if size is None:
            buf += self.fileobj.read()
        else:
            buf += self.fileobj.read(size - len(buf))

        self.position += len(buf)
        return buf

    def readline(self, size=-1):
        """Read one entire line from the file. If size is present
           and non-negative, return a string with at most that
           size, which may be an incomplete line.
           Lines are split by \n, CR is not automatically removed.
        """
        if self.closed:
            raise ValueError("I/O operation on closed file")

        if b"\n" in self.buffer:
            pos = self.buffer.find(b"\n") + 1
        else:
            buffers = [self.buffer]
            while True:
                buf = self.fileobj.read(self.blocksize)
                buffers.append(buf)
                if not buf or b"\n" in buf:
                    self.buffer = b"".join(buffers)
                    pos = self.buffer.find(b"\n") + 1
                    if pos == 0:
                        # no newline found.
                        pos = len(self.buffer)
                    break

        if size != -1:
            pos = min(size, pos)

        buf = self.buffer[:pos]
        self.buffer = self.buffer[pos:]
        self.position += len(buf)
        return six.ensure_text(buf)

    def readlines(self):
        """Return a list with all remaining lines.
        """
        result = []
        while True:
            line = self.readline()
            if not line:
                break
            result.append(line)
        return result

    def tell(self):
        """Return the current file position.
        """
        if self.closed:
            raise ValueError("I/O operation on closed file")

        return self.position

    def seek(self, pos, whence=os.SEEK_SET):
        """Seek to a position in the file.
        """
        if self.closed:
            raise ValueError("I/O operation on closed file")

        if whence == os.SEEK_SET:
            self.position = min(max(pos, 0), self.size)
        elif whence == os.SEEK_CUR:
            if pos < 0:
                self.position = max(self.position + pos, 0)
            else:
                self.position = min(self.position + pos, self.size)
        elif whence == os.SEEK_END:
            self.position = max(min(self.size + pos, self.size), 0)
        else:
            raise ValueError("Invalid argument")

        self.buffer = b""
        self.fileobj.seek(self.position)

    def close(self):
        """Close the file object.
        """
        self.closed = True

    def __iter__(self):
        """Get an iterator over the file's lines.
        """
        while True:
            line = self.readline()
            if not line:
                break
            yield line
#class ExFileObject

#------------------
# Exported Classes
#------------------
class CpioInfo(object):
    """Informational class which holds the details about an
       archive member given by a cpio header block.
       CpioInfo objects are returned by CpioFile.getmember(),
       CpioFile.getmembers() and CpioFile.getcpioinfo() and are
       usually created internally.
    """

    def __init__(self, name=""):
        """Construct a CpioInfo object. name is the optional name
           of the member.
        """
        self.ino = 0            # i-node
        self.mode = S_IFREG | 0o444
        self.uid = 0            # user id
        self.gid = 0            # group id
        self.nlink = 1          # number of links
        self.mtime = 0          # modification time
        self.size = 0           # file size
        self.devmajor = 0       # device major number
        self.devminor = 0       # device minor number
        self.rdevmajor = 0
        self.rdevminor = 0
        self.namesize = 0
        self.check = 0

        self.name = name
        self.linkname = ''

        self.offset = 0         # the cpio header starts here
        self.offset_data = 0    # the file's data starts here

        self.buf = None

    def __repr__(self):
        return "<%s %r at %#x>" % (self.__class__.__name__, self.name, id(self))

    @classmethod
    def frombuf(cls, buf):
        """Construct a CpioInfo object from a string buffer.
        """
        cpioinfo = cls()
        cpioinfo.buf = buf

        cpioinfo.ino = int(buf[6:14], 16)
        cpioinfo.mode = int(buf[14:22], 16)
        cpioinfo.uid = int(buf[22:30], 16)
        cpioinfo.gid = int(buf[30:38], 16)
        cpioinfo.nlink = int(buf[38:46], 16)
        cpioinfo.mtime = int(buf[46:54], 16)
        cpioinfo.size = int(buf[54:62], 16)
        cpioinfo.devmajor = int(buf[62:70], 16)
        cpioinfo.devminor = int(buf[70:78], 16)
        cpioinfo.rdevmajor = int(buf[78:86], 16)
        cpioinfo.rdevminor = int(buf[86:94], 16)
        cpioinfo.namesize = int(buf[94:102], 16)
        cpioinfo.check = int(buf[102:110], 16)

        return cpioinfo

    def tobuf(self):
        """Return a cpio header as bytes"""
        buf = b"%06X" % MAGIC_NEWC
        buf += b"%08X" % self.ino
        buf += b"%08X" % self.mode
        buf += b"%08X" % self.uid
        buf += b"%08X" % self.gid
        buf += b"%08X" % self.nlink
        buf += b"%08X" % int(self.mtime)
        buf += b"%08X" % (self.linkname == '' and self.size or
                         len(self.linkname))
        buf += b"%08X" % self.devmajor
        buf += b"%08X" % self.devminor
        buf += b"%08X" % self.rdevmajor
        buf += b"%08X" % self.rdevminor
        buf += b"%08X" % (len(self.name)+1)
        buf += b"%08X" % self.check

        buf += six.ensure_binary(self.name) + NUL
        _, remainder = divmod(len(buf), WORDSIZE)
        if remainder != 0:
            # pad to next word
            buf += (WORDSIZE - remainder) * NUL

        if self.linkname != '':
            buf += six.ensure_binary(self.linkname)
            _, remainder = divmod(len(buf), WORDSIZE)
            if remainder != 0:
                # pad to next word
                buf += (WORDSIZE - remainder) * NUL

        self.buf = buf
        return buf

    def isreg(self):
        return stat.S_ISREG(self.mode)
    def isfile(self):
        return self.isreg()
    def isdir(self):
        return stat.S_ISDIR(self.mode)
    def issym(self):
        return stat.S_ISLNK(self.mode)
    def islnk(self):
        return (stat.S_ISREG(self.mode) and self.nlink > 1)
    def ischr(self):
        return stat.S_ISCHR(self.mode)
    def isblk(self):
        return stat.S_ISBLK(self.mode)
    def isfifo(self):
        return stat.S_ISFIFO(self.mode)
    def issparse(self):
        return False
    def isdev(self):
        return (stat.S_ISCHR(self.mode) or stat.S_ISBLK(self.mode))
# class CpioInfo

class CpioFile(six.Iterator):
    """The CpioFile Class provides an interface to cpio archives.
    """

    debug = 0                   # May be set from 0 (no msgs) to 3 (all msgs)

    dereference = False         # If true, add content of linked file to the
                                # cpio file, else the link.

    hardlinks = True		# If true, only add content for the first
    				# hard link, else treat as regular file.

    errorlevel = 0              # If 0, fatal errors only appear in debug
                                # messages (if debug >= 0). If > 0, errors
                                # are passed to the caller as exceptions.

    fileobject = ExFileObject

    def __init__(self, name=None, mode="r", fileobj=None):
        # type:(str | None, str, Optional[IO[bytes] | GzipFile | _Stream]) -> None
        """Open an (uncompressed) cpio archive `name'. `mode' is either 'r' to
           read from an existing archive, 'a' to append data to an existing
           file or 'w' to create a new file overwriting an existing one. `mode'
           defaults to 'r'.
           If `fileobj' is given, it is used for reading or writing data. If it
           can be determined, `mode' is overridden by `fileobj's mode.
           `fileobj' is not closed, when CpioFile is closed.
        """
        if len(mode) > 1 or mode not in "raw":
            raise ValueError("mode must be 'r', 'a' or 'w'")
        self._mode = mode
        self.mode = {"r": "rb", "a": "r+b", "w": "wb"}[mode]

        if not fileobj:
            assert name
            fileobj = bltn_open(name, self.mode)
            self._extfileobj = False
        else:
            if name is None and hasattr(fileobj, "name"):
                name = fileobj.name
            if hasattr(fileobj, "mode"):
                self.mode = cast(str, fileobj.mode)
            self._extfileobj = True
        self.name = None
        if name:
            self.name = os.path.abspath(name)
        assert not isinstance(fileobj, io.TextIOBase)
        self.fileobj = fileobj

        # Init datastructures
        self.closed = False
        self.members = []       # type:list[CpioInfo]
        self._loaded = False    # flag if all members have been read
        self.offset = 0        # current position in the archive file
        self.inodes = {}        # dictionary caching the inodes of
                                # archive members already added

        if self._mode == "r":
            self.firstmember = None
            self.firstmember = next(self)

        if self._mode == "a":
            # Move to the end of the archive,
            # before the trailer.
            self.firstmember = None
            last_offset = 0
            while True:
                try:
                    cpioinfo = next(self)
                except ReadError:
                    self.fileobj.seek(0)
                    break
                if cpioinfo is None:
                    self.fileobj.seek(last_offset)
                    break
                else:
                    last_offset = cpioinfo.offset

        if self._mode in "aw":
            self._loaded = True

    #--------------------------------------------------------------------------
    # Below are the classmethods which act as alternate constructors to the
    # CpioFile class. The open() method is the only one that is needed for
    # public use; it is the "super"-constructor and is able to select an
    # adequate "sub"-constructor for a particular compression using the mapping
    # from OPEN_METH.
    #
    # This concept allows one to subclass CpioFile without losing the comfort of
    # the super-constructor. A sub-constructor is registered and made available
    # by adding it to the mapping in OPEN_METH.

    @classmethod
    def open(cls, name=None, mode="r", fileobj=None, bufsize=20*512):
        """Open a cpio archive for reading, writing or appending. Return
           an appropriate CpioFile class.

           mode:
           'r' or 'r:*' open for reading with transparent compression
           'r:'         open for reading exclusively uncompressed
           'r:gz'       open for reading with gzip compression
           'r:bz2'      open for reading with bzip2 compression
           'r:xz'       open for reading with xz compression
           'a' or 'a:'  open for appending
           'w' or 'w:'  open for writing without compression
           'w:gz'       open for writing with gzip compression
           'w:bz2'      open for writing with bzip2 compression
           'w:xz'       open for writing with xz compression

           'r|*'        open a stream of cpio blocks with transparent compression
           'r|'         open an uncompressed stream of cpio blocks for reading
           'r|gz'       open a gzip compressed stream of cpio blocks
           'r|bz2'      open a bzip2 compressed stream of cpio blocks
           'r|xz'       open a xz compressed stream of cpio blocks
           'w|'         open an uncompressed stream for writing
           'w|gz'       open a gzip compressed stream for writing
           'w|bz2'      open a bzip2 compressed stream for writing
           'w|xz'       open a xz compressed stream for writing
        """

        if not name and not fileobj:
            raise ValueError("nothing to open")
        if fileobj:
            if sys.version_info < (3, 0):
                if isinstance(fileobj, io.StringIO):
                    raise TypeError("CpioFile.fileobj does not support io.StringIO()")
            else:
                if not isinstance(fileobj, io.IOBase) or isinstance(fileobj, io.TextIOBase):
                    raise TypeError("CpioFile.fileobj needs to be IO[bytes] or io.BytesIO()")

        if mode in ("r", "r:*"):
            # Find out which *open() is appropriate for opening the file.
            for comptype in cls.OPEN_METH:
                func = getattr(cls, cls.OPEN_METH[comptype])
                if fileobj is not None:
                    saved_pos = fileobj.tell()
                try:
                    return func(name, "r", fileobj)
                except (ReadError, CompressionError):
                    if fileobj is not None:
                        fileobj.seek(saved_pos)
                    continue
            raise ReadError("file could not be opened successfully")

        elif ":" in mode:
            fmode, comptype = mode.split(":", 1)
            fmode = fmode or "r"
            comptype = comptype or "cpio"

            # Select the *open() function according to
            # given compression.
            if comptype in cls.OPEN_METH:
                func = getattr(cls, cls.OPEN_METH[comptype])
            else:
                raise CompressionError("unknown compression type %r" % comptype)
            return func(name, fmode, fileobj)

        elif "|" in mode:
            fmode, comptype = mode.split("|", 1)
            fmode = fmode or "r"
            comptype = comptype or "cpio"

            if fmode not in "rw":
                raise ValueError("mode must be 'r' or 'w'")

            t = cls(name, fmode,
                    _Stream(name, fmode, comptype, fileobj, bufsize))
            t._extfileobj = False
            return t

        elif mode in "aw":
            return cls.cpioopen(name, mode, fileobj)

        raise ValueError("undiscernible mode")

    @classmethod
    def cpioopen(cls, name, mode="r", fileobj=None):
        # type:(str, str, Optional[GzipFile | IO[bytes]]) -> CpioFile
        """Open uncompressed cpio archive name for reading or writing."""
        if len(mode) > 1 or mode not in "raw":
            raise ValueError("mode must be 'r', 'a' or 'w'")
        return cls(name, mode, fileobj)

    @classmethod
    def gzopen(cls, name, mode="r", fileobj=None, compresslevel=9):
        """Open gzip compressed cpio archive name for reading or writing.
           Appending is not allowed.
        """
        if len(mode) > 1 or mode not in "rw":
            raise ValueError("mode must be 'r' or 'w'")
        try:
            t = cls.cpioopen(name, mode, gzip.GzipFile(name, mode + "b", compresslevel, fileobj))
        except IOError:
            raise ReadError("not a gzip file")
        t._extfileobj = False
        return t

    @classmethod
    def bz2open(cls, name, mode="r", fileobj=None, compresslevel=9):
        # type:(str, Literal["r", "w"], Optional[IO[bytes]], int) -> CpioFile
        """Open bzip2 compressed cpio archive name for reading or writing, no appending"""
        if len(mode) > 1 or mode not in "rw":
            raise ValueError("mode must be 'r' or 'w'.")

        if fileobj is not None:
            fileobj = cast(IO[Any], _BZ2Proxy(fileobj, mode))  # pragma: no cover
        else:
            fileobj = bz2.BZ2File(name, mode, compresslevel=compresslevel)

        try:
            t = cls.cpioopen(name, mode, fileobj)
        except IOError:
            raise ReadError("not a bzip2 file")
        t._extfileobj = False
        return t

    @classmethod
    def xzopen(cls, name, mode="r", fileobj=None, compresslevel=6):
        # type:(str, Literal["r", "w"], Optional[IO[bytes]], int) -> CpioFile
        """
        Open xz compressed cpio archive name for reading or writing.
        Appending is not allowed.
        """
        if len(mode) > 1 or mode not in "rw":
            raise ValueError("mode must be 'r' or 'w'.")

        try:
            import lzma
        except ImportError:
            raise CompressionError("lzma module is not available")

        if fileobj is not None:
            raise CompressionError("passing fileobj not implemented for LZMA")
        kwargs = {}
        if sys.version_info < (3, 0):
            kwargs["options"] = {"level": compresslevel}
        elif "w" in mode:
            kwargs["preset"] = compresslevel
        fileobj = lzma.LZMAFile(name, mode, **cast(Any, kwargs))
        try:
            t = cls.cpioopen(name, mode, fileobj)
        except IOError:
            raise ReadError("not a XZ file")
        t._extfileobj = False
        return t

    # All *open() methods are registered here.
    OPEN_METH = {
        "cpio": "cpioopen",   # uncompressed cpio
        "gz":  "gzopen",    # gzip compressed cpio
        "bz2": "bz2open",   # bzip2 compressed cpio
        "xz":  "xzopen",  # xz compressed cpio
    }

    #--------------------------------------------------------------------------
    # The public methods which CpioFile provides:

    def close(self):
        """Close the CpioFile. In write-mode, a trailer record is
           appended to the archive.
        """
        if self.closed:
            return

        if self._mode in "aw":
            trailer = CpioInfo(TRAILER_NAME)
            trailer.mode = 0
            buf = trailer.tobuf()
            self.fileobj.write(buf)
            self.offset += len(buf)

        if not self._extfileobj:
            self.fileobj.close()
        self.closed = True

    def getmember(self, name):
        # type:(str | bytes) -> CpioInfo
        """Return a CpioInfo object for member `name'. If `name' can not be
           found in the archive, KeyError is raised. If a member occurs more
           than once in the archive, its last occurence is assumed to be the
           most up-to-date version.
        """
        cpioinfo = self._getmember(name)
        if cpioinfo is None:
            raise KeyError("filename %r not found" % name)
        return cpioinfo

    def getmembers(self):
        # type:() -> List[CpioInfo]
        """Return the members of the archive as a list of CpioInfo objects. The
           list has the same order as the members in the archive.
        """
        self._check()
        if not self._loaded:    # if we want to obtain a list of
            self._load()        # all members, we first have to
                                # scan the whole archive.
        return self.members

    def getnames(self):
        """Return the members of the archive as a list of their names. It has
           the same order as the list returned by getmembers().
        """
        return [cpioinfo.name for cpioinfo in self.getmembers()]

    def getcpioinfo(self, name=None, arcname=None, fileobj=None):
        """Create a CpioInfo object for either the file `name' or the file
           object `fileobj' (using os.fstat on its file descriptor). You can
           modify some of the CpioInfo's attributes before you add it using
           addfile(). If given, `arcname' specifies an alternative name for the
           file in the archive.
        """
        self._check("aw")

        # When fileobj is given, replace name by
        # fileobj's real name.
        if fileobj is not None:
            name = fileobj.name

        # Building the name of the member in the archive.
        # Backward slashes are converted to forward slashes,
        # Absolute paths are turned to relative paths.
        if arcname is None:
            arcname = name
        arcname = normpath(arcname)
        _, arcname = os.path.splitdrive(arcname)
        while arcname[0:1] == "/":
            arcname = arcname[1:]

        # Now, fill the CpioInfo object with
        # information specific for the file.
        cpioinfo = CpioInfo()

        # Use os.stat or os.lstat, depending on platform
        # and if symlinks shall be resolved.
        if fileobj is None:
            if hasattr(os, "lstat") and not self.dereference:
                statres = os.lstat(name)
            else:
                statres = os.stat(name)
        else:
            statres = os.fstat(fileobj.fileno())

        stmd = statres.st_mode

        # Fill the CpioInfo object with all
        # information we can get.
        cpioinfo.ino = statres.st_ino
        cpioinfo.mode = stmd
        cpioinfo.uid = statres.st_uid
        cpioinfo.gid = statres.st_gid
        cpioinfo.nlink = statres.st_nlink
        cpioinfo.mtime = statres.st_mtime
        if stat.S_ISREG(stmd):
            cpioinfo.size = statres.st_size
        else:
            cpioinfo.size = 0
        cpioinfo.devmajor = os.major(statres.st_dev)
        cpioinfo.devminor = os.minor(statres.st_dev)
        if stat.S_ISCHR(stmd) or stat.S_ISBLK(stmd):
            cpioinfo.rdevmajor = os.major(statres.st_rdev)
            cpioinfo.rdevminor = os.minor(statres.st_rdev)
        if stat.S_ISLNK(stmd):
            cpioinfo.linkname = os.readlink(name)
        cpioinfo.namesize = len(arcname)
        cpioinfo.name = six.ensure_str(arcname)

        return cpioinfo

    def list(self, verbose=True):
        """Print a table of contents to sys.stdout. If `verbose' is False, only
           the names of the members are printed. If it is True, an `ls -l'-like
           output is produced.
        """
        self._check()

        for cpioinfo in self:
            if verbose:
                print(filemode(cpioinfo.mode), end=' ')
                print("%d/%d" % (cpioinfo.uid, cpioinfo.gid), end=' ')
                if cpioinfo.ischr() or cpioinfo.isblk():
                    print("%10s" % ("%d,%d" % (cpioinfo.devmajor, cpioinfo.devminor)), end=' ')
                else:
                    print("%10d" % cpioinfo.size, end=' ')
                print("%d-%02d-%02d %02d:%02d:%02d" % time.localtime(cpioinfo.mtime)[:6], end=' ')

            print(cpioinfo.name, end="")

            if verbose:
                if cpioinfo.issym():
                    print("->", cpioinfo.linkname, end="")
                if cpioinfo.islnk():
                    print("link to", cpioinfo.linkname, end="")
            print()

    def add(self, name, arcname=None, recursive=True):
        """Add the file `name' to the archive. `name' may be any type of file
           (directory, fifo, symbolic link, etc.). If given, `arcname'
           specifies an alternative name for the file in the archive.
           Directories are added recursively by default. This can be avoided by
           setting `recursive' to False.
        """
        self._check("aw")

        if arcname is None:
            arcname = name

        # Skip if somebody tries to archive the archive...
        if self.name is not None and os.path.abspath(name) == self.name:
            self._dbg(2, "cpiofile: Skipped %r" % name)
            return

        # Special case: The user wants to add the current
        # working directory.
        if name == ".":
            if recursive:
                if arcname == ".":
                    arcname = ""
                for f in os.listdir("."):
                    self.add(f, os.path.join(arcname, f))
            return

        self._dbg(1, name)

        # Create a CpioInfo object from the file.
        cpioinfo = self.getcpioinfo(name, arcname)

        if cpioinfo is None:
            self._dbg(1, "cpiofile: Unsupported type %r" % name)
            return

        # Append the cpio header and data to the archive.
        if cpioinfo.isreg():
            f = bltn_open(name, "rb")
            self.addfile(cpioinfo, f)
            f.close()

        elif cpioinfo.isdir():
            self.addfile(cpioinfo)
            if recursive:
                for f in os.listdir(name):
                    self.add(os.path.join(name, f), os.path.join(arcname, f))

        else:
            self.addfile(cpioinfo)

    def addfile(self, cpioinfo, fileobj=None):
        """Add the CpioInfo object `cpioinfo' to the archive. If `fileobj' is
           given, cpioinfo.size bytes are read from it and added to the archive.
           You can create CpioInfo objects using getcpioinfo().
           On Windows platforms, `fileobj' should always be opened with mode
           'rb' to avoid irritation about the file size.
        """
        self._check("aw")

        cpioinfo = copy.copy(cpioinfo)

        if cpioinfo.nlink > 1:
            if self.hardlinks and cpioinfo.ino in self.inodes:
                # this inode has already been added
                cpioinfo.size = 0
                self.inodes[cpioinfo.ino].append(cpioinfo.name)
            else:
                self.inodes[cpioinfo.ino] = [cpioinfo.name]

        buf = cpioinfo.tobuf()
        self.fileobj.write(buf)
        self.offset += len(buf)

        # If there's data to follow, append it.
        if fileobj is not None:
            copyfileobj(fileobj, self.fileobj, cpioinfo.size)
            self.offset += cpioinfo.size

            _, remainder = divmod(self.offset, WORDSIZE)
            if remainder > 0:
                # pad to next word
                self.fileobj.write((WORDSIZE - remainder) * NUL)
                self.offset += (WORDSIZE - remainder)

        self.members.append(cpioinfo)

    def extractall(self, path=".", members=None):
        """Extract all members from the archive to the current working
           directory and set owner, modification time and permissions on
           directories afterwards. `path' specifies a different directory
           to extract to. `members' is optional and must be a subset of the
           list returned by getmembers().
        """
        directories = []

        if members is None:
            members = self

        for cpioinfo in members:
            if cpioinfo.isdir():
                # Extract directory with a safe mode, so that
                # all files below can be extracted as well.
                try:
                    os.makedirs(os.path.join(path, six.ensure_text(cpioinfo.name)), 0o777)
                except EnvironmentError:
                    pass
                directories.append(cpioinfo)
            else:
                self.extract(cpioinfo, path)

        # Reverse sort directories.
        directories.sort(key=lambda x: x.name)
        directories.reverse()

        # Set correct owner, mtime and filemode on directories.
        for cpioinfo in directories:
            path = os.path.join(path, six.ensure_text(cpioinfo.name))
            try:
                self.chown(cpioinfo, path)
                self.utime(cpioinfo, path)
                self.chmod(cpioinfo, path)
            except ExtractError as e:
                if self.errorlevel > 1:
                    raise
                else:
                    self._dbg(1, "cpiofile: %s" % e)

    def extract(self, member, path=""):
        """Extract a member from the archive to the current working directory,
           using its full name. Its file information is extracted as accurately
           as possible. `member' may be a filename or a CpioInfo object. You can
           specify a different directory using `path'.
        """
        self._check("r")

        if isinstance(member, CpioInfo):
            cpioinfo = member
        else:
            cpioinfo = self.getmember(member)

        # Prepare the link target for makelink().
        if cpioinfo.islnk():
            cpioinfo._link_path = path

        try:
            self._extract_member(cpioinfo, os.path.join(path, six.ensure_text(cpioinfo.name)))
        except EnvironmentError as e:
            if self.errorlevel > 0:
                raise
            else:
                if e.filename is None:
                    self._dbg(1, "cpiofile: %s" % e.strerror)
                else:
                    self._dbg(1, "cpiofile: %s %r" % (e.strerror, e.filename))
        except ExtractError as e:
            if self.errorlevel > 1:
                raise
            else:
                self._dbg(1, "cpiofile: %s" % e)

    def extractfile(self, member):
        # type:(CpioInfo) -> ExFileObject | None
        """Extract a member from the archive as a file object. `member' may be
           a filename or a CpioInfo object. If `member' is a regular file, a
           file-like object is returned. If `member' is a link, a file-like
           object is constructed from the link's target. If `member' is none of
           the above, None is returned.
           The file-like object is read-only and provides the following
           methods: read(), readline(), readlines(), seek() and tell()
        """
        self._check("r")

        if isinstance(member, CpioInfo):
            cpioinfo = member
        else:
            cpioinfo = self.getmember(member)

        if cpioinfo.isreg():
            return self.fileobject(self, cpioinfo)

        elif cpioinfo.islnk():
            return self.fileobject(self, self._datamember(cpioinfo))
        elif cpioinfo.issym():
            if isinstance(self.fileobj, _Stream):
                # A small but ugly workaround for the case that someone tries
                # to extract a symlink as a file-object from a non-seekable
                # stream of cpio blocks.
                raise StreamError("cannot extract symlink as file object")
            else:
                # A symlink's file object is its target's file object.
                return self.extractfile(self._getmember(cpioinfo.linkname,
                                                        cpioinfo))  # type: ignore
        else:
            # If there's no data associated with the member (directory, chrdev,
            # blkdev, etc.), return None instead of a file object.
            return None

    def _extract_member(self, cpioinfo, targetpath):
        """Extract the CpioInfo object cpioinfo to a physical
           file called targetpath.
        """
        # Fetch the CpioInfo object for the given name
        # and build the destination pathname, replacing
        # forward slashes to platform specific separators.
        targetpath = os.path.normpath(targetpath)

        # Create all upper directories.
        upperdirs = os.path.dirname(targetpath)
        if upperdirs and not os.path.exists(upperdirs):
            ti = CpioInfo()
            ti.name  = upperdirs
            ti.mode  = S_IFDIR | 0o777
            ti.mtime = cpioinfo.mtime
            ti.uid   = cpioinfo.uid
            ti.gid   = cpioinfo.gid
            try:
                self._extract_member(ti, ti.name)
            except Exception:
                pass

        if cpioinfo.issym():
            self._dbg(1, "%s -> %s" % (cpioinfo.name, cpioinfo.linkname))
        else:
            self._dbg(1, cpioinfo.name)

        if cpioinfo.isreg():
            self.makefile(cpioinfo, targetpath)
        elif cpioinfo.isdir():
            self.makedir(cpioinfo, targetpath)
        elif cpioinfo.isfifo():
            self.makefifo(cpioinfo, targetpath)
        elif cpioinfo.ischr() or cpioinfo.isblk():
            self.makedev(cpioinfo, targetpath)
        elif cpioinfo.issym():
            self.makesymlink(cpioinfo, targetpath)
        else:
            self.makefile(cpioinfo, targetpath)

        self.chown(cpioinfo, targetpath)
        if not cpioinfo.issym():
            self.chmod(cpioinfo, targetpath)
            self.utime(cpioinfo, targetpath)

    #--------------------------------------------------------------------------
    # Below are the different file methods. They are called via
    # _extract_member() when extract() is called. They can be replaced in a
    # subclass to implement other functionality.

    def makedir(self, cpioinfo, targetpath):
        """Make a directory called targetpath.
        """
        try:
            os.mkdir(targetpath)
        except EnvironmentError as e:
            if e.errno != errno.EEXIST:
                raise

    def makefile(self, cpioinfo, targetpath):
        """Make a file called targetpath.
        """
        extractinfo = None
        if cpioinfo.nlink == 1:
            extractinfo = cpioinfo
        else:
            if cpioinfo.ino in self.inodes:
                # actual file exists, create link
                os.link(os.path.join(cpioinfo._link_path,
                                     six.ensure_text(self.inodes[cpioinfo.ino][0])), targetpath)
            else:
                extractinfo = self._datamember(cpioinfo)

        if cpioinfo.ino not in self.inodes:
            self.inodes[cpioinfo.ino] = []
        self.inodes[cpioinfo.ino].append(cpioinfo.name)

        if extractinfo:
            source = self.extractfile(extractinfo)
            target = bltn_open(targetpath, "wb")
            copyfileobj(source, target)
            cast(ExFileObject, source).close()
            target.close()

    def makefifo(self, cpioinfo, targetpath):
        """Make a fifo called targetpath.
        """
        if hasattr(os, "mkfifo"):
            os.mkfifo(targetpath)
        else:
            raise ExtractError("fifo not supported by system")

    def makedev(self, cpioinfo, targetpath):
        """Make a character or block device called targetpath.
        """
        if not hasattr(os, "mknod") or not hasattr(os, "makedev"):
            raise ExtractError("special devices not supported by system")

        mode = cpioinfo.mode
        if cpioinfo.isblk():
            mode |= stat.S_IFBLK
        else:
            mode |= stat.S_IFCHR

        os.mknod(targetpath, mode,
                 os.makedev(cpioinfo.devmajor, cpioinfo.devminor))

    def makesymlink(self, cpioinfo, targetpath):
        os.symlink(cpioinfo.linkname, targetpath)

    def makelink(self, cpioinfo, targetpath):
        """Make a (symbolic) link called targetpath. If it cannot be created
          (platform limitation), we try to make a copy of the referenced file
          instead of a link.
        """
        linkpath = cpioinfo.linkname
        try:
            if cpioinfo.issym():
                os.symlink(linkpath, targetpath)
            else:
                # See extract().
                os.link(cpioinfo._link_target, targetpath)
        except AttributeError:
            if cpioinfo.issym():
                linkpath = os.path.join(os.path.dirname(cpioinfo.name),
                                        linkpath)
                linkpath = normpath(linkpath)

            try:
                self._extract_member(self.getmember(linkpath), targetpath)
            except (EnvironmentError, KeyError):
                linkpath = os.path.normpath(linkpath)
                try:
                    shutil.copy2(linkpath, targetpath)
                except EnvironmentError:
                    raise IOError("link could not be created")

    def chown(self, cpioinfo, targetpath):
        """Set owner of targetpath according to cpioinfo.
        """
        if PWD and hasattr(os, "geteuid") and os.geteuid() == 0:
            # We have to be root to do so.
            try:
                g = GRP.getgrgid(cpioinfo.gid)[2]
            except KeyError:
                g = os.getgid()
            try:
                u = PWD.getpwuid(cpioinfo.uid)[2]
            except KeyError:
                u = os.getuid()
            try:
                if cpioinfo.issym() and hasattr(os, "lchown"):
                    os.lchown(targetpath, u, g)
                else:
                    if sys.platform != "os2emx":
                        os.chown(targetpath, u, g)
            except EnvironmentError:
                raise ExtractError("could not change owner")

    def chmod(self, cpioinfo, targetpath):
        """Set file permissions of targetpath according to cpioinfo.
        """
        if hasattr(os, 'chmod'):
            try:
                os.chmod(targetpath, cpioinfo.mode)
            except EnvironmentError:
                raise ExtractError("could not change mode")

    def utime(self, cpioinfo, targetpath):
        """Set modification time of targetpath according to cpioinfo.
        """
        if not hasattr(os, 'utime'):
            return
        if sys.platform == "win32" and cpioinfo.isdir():
            # According to msdn.microsoft.com, it is an error (EACCES)
            # to use utime() on directories.
            return
        try:
            os.utime(targetpath, (cpioinfo.mtime, cpioinfo.mtime))
        except EnvironmentError:
            raise ExtractError("could not change modification time")

    #--------------------------------------------------------------------------
    def __next__(self):
        """Return the next member of the archive as a CpioInfo object, when
           CpioFile is opened for reading. Return None if there is no more
           available.
        """
        self._check("ra")
        if self.firstmember is not None:
            m = self.firstmember
            self.firstmember = None
            return m

        # Read the next block.
        self.fileobj.seek(self.offset)
        buf = self.fileobj.read(HEADERSIZE_SVR4)
        if not buf:
            return None

        try:
            cpioinfo = CpioInfo.frombuf(buf)
            total_header_len = self._word(HEADERSIZE_SVR4 + cpioinfo.namesize)
            name_buf = self.fileobj.read(total_header_len - HEADERSIZE_SVR4)
            name = name_buf.rstrip(NUL)

            if name == TRAILER_NAME:
                self.offset += total_header_len
                return None
            cpioinfo.name = six.ensure_str(name)

            # Set the CpioInfo object's offset to the current position of the
            # CpioFile and set self.offset to the position where the data blocks
            # should begin.
            cpioinfo.offset = self.offset
            self.offset += total_header_len

            if cpioinfo.issym():
                linkname_buf = self.fileobj.read(self._word(cpioinfo.size))
                cpioinfo.linkname = six.ensure_text(linkname_buf.rstrip(NUL))
                self.offset += self._word(cpioinfo.size)
                cpioinfo.size = 0

            cpioinfo = self.proc_member(cpioinfo)

        except ValueError as e:
            if self.offset == 0:
                raise ReadError("empty, unreadable or compressed "
                                "file: %s" % e)
            return None

        self.members.append(cpioinfo)
        return cpioinfo

    def proc_member(self, cpioinfo):
        """Process a builtin type member or an unknown member
           which will be treated as a regular file.
        """
        cpioinfo.offset_data = self.offset
        if cpioinfo.size > 0:
            # Skip the following data blocks.
            self.offset += self._word(cpioinfo.size)
        return cpioinfo

    #--------------------------------------------------------------------------
    # Little helper methods:

    def _word(self, count):
        """Round up a byte count by WORDSIZE and return it,
           e.g. _word(17) => 20.
        """
        words, remainder = divmod(count, WORDSIZE)
        if remainder:
            words += 1
        return words * WORDSIZE

    def _datamember(self, cpioinfo):
        """Find the archive member that actually has the data
           for cpioinfo.ino.
        """
        if cpioinfo.size == 0:
            # perhaps another member has the data?
            for info in self:
                if info.ino == cpioinfo.ino and info.size > 0:
                    self._dbg(2, "cpiofile: found member %s" % info.name)
                    return info

        return cpioinfo

    def _getmember(self, name, cpioinfo=None):
        # type:(str | bytes, CpioInfo | None) -> CpioInfo | None
        """Find an archive member by name from bottom to top.
           If cpioinfo is given, it is used as the starting point.
        """
        # Ensure that all members have been loaded.
        members = self.getmembers()

        if cpioinfo is None:
            end = len(members)
        else:
            end = members.index(cpioinfo)

        encoded_name = six.ensure_str(name)
        for i in range(end - 1, -1, -1):
            if encoded_name == members[i].name:
                return members[i]
        return None  # pragma: no cover

    def _load(self):
        """Read through the entire archive file and look for readable
           members.
        """
        while True:
            cpioinfo = next(self)
            if cpioinfo is None:
                break
        self._loaded = True

    def _check(self, mode=None):
        """Check if CpioFile is still open, and if the operation's mode
           corresponds to CpioFile's mode.
        """
        if self.closed:
            raise IOError("%s is closed" % self.__class__.__name__)
        if mode is not None and self._mode not in mode:
            raise IOError("bad operation for mode %r" % self._mode)

    def __iter__(self):
        """Provide an iterator object.
        """
        if self._loaded:
            return iter(self.members)
        else:
            return CpioIter(self)

    def _dbg(self, level, msg):
        """Write debugging output to sys.stderr.
        """
        if level <= self.debug:
            print(msg, file=sys.stderr)

    # Context manager protocol methods added by ntherning

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()
# class CpioFile

class CpioIter(six.Iterator):
    """Iterator Class.

       for cpioinfo in CpioFile(...):
           suite...
    """

    def __init__(self, cpiofile):
        """Construct a CpioIter object.
        """
        self.cpiofile = cpiofile
        self.index = 0
    def __iter__(self):
        """Return iterator object.
        """
        return self
    def __next__(self):
        """Return the next item using CpioFile's next() method.
           When all members have been read, set CpioFile as _loaded.
        """
        # Fix for SF #1100429: Under rare circumstances it can
        # happen that getmembers() is called during iteration,
        # which will cause CpioIter to stop prematurely.
        if not self.cpiofile._loaded:
            cpioinfo = next(self.cpiofile)
            if not cpioinfo:
                self.cpiofile._loaded = True
                raise StopIteration
        else:
            try:
                cpioinfo = self.cpiofile.members[self.index]
            except IndexError:
                raise StopIteration
        self.index += 1
        return cpioinfo

#---------------------------------------------
# zipfile compatible CpioFile class
#---------------------------------------------
CPIO_PLAIN = 0           # zipfile.ZIP_STORED
CPIO_GZIPPED = 8         # zipfile.ZIP_DEFLATED
class CpioFileCompat(object):
    """CpioFile class compatible with standard module zipfile's
       ZipFile class.
    """
    def __init__(self, fpath, mode="r", compression=CPIO_PLAIN):
        # type:(str, Literal["r", "w"], int) -> None
        if compression == CPIO_PLAIN:
            self.cpiofile = CpioFile.cpioopen(fpath, mode)
        elif compression == CPIO_GZIPPED:
            self.cpiofile = CpioFile.gzopen(fpath, mode)
        else:
            raise ValueError("unknown compression constant")
    def namelist(self):
        return [m.name for m in self.infolist()]
    def infolist(self):
        return [m for m in self.cpiofile.getmembers() if m.isreg()]
    def printdir(self):
        self.cpiofile.list()
    def testzip(self):
        return
    def getinfo(self, name):
        return self.cpiofile.getmember(name)
    def read(self, name):
        cpioinfo = self.cpiofile.getmember(name)
        assert cpioinfo
        return cast(ExFileObject, self.cpiofile.extractfile(cpioinfo)).read()
    def write(self, filename, arcname=None, compress_type=None):
        self.cpiofile.add(filename, arcname)
    # deleted writestr method
    def close(self):
        self.cpiofile.close()
#class CpioFileCompat

#--------------------
# exported functions
#--------------------
def is_cpiofile(name):
    """Return True if name points to a cpio archive that we
       are able to handle, else return False.
    """
    try:
        t = open(name)
        t.close()
        return True
    except CpioError:
        return False

bltn_open = open
open = CpioFile.open  # pylint: disable=redefined-builtin
