#!/usr/bin/env python3
"""Re-apply NOTE phdr patch to Copilot CLI ELF binary.

UEK kernel 6.12 rejects the binary due to an oversized NOTE segment
(~34MB overlapping the Node.js SEA blob). This shrinks the NOTE program
header's filesz/memsz to 0x200.

The patch is idempotent — if filesz is already <= 0x200, it skips.
npm auto-update restores the original binary, so this must run after every update.
"""
import struct
import sys
from pathlib import Path

BINARY = Path(
    "/home/opc/.local/lib/node_modules/@github/copilot/"
    "node_modules/@github/copilot-linux-arm64/copilot"
)
TARGET_SIZE = 0x200


def patch_elf_note_segment() -> bool:
    with open(BINARY, "r+b") as f:
        elf_header = f.read(64)
        if elf_header[:4] != b"\x7fELF":
            print(f"ERROR: {BINARY} is not an ELF file", file=sys.stderr)
            return False

        e_phoff = struct.unpack_from("<Q", elf_header, 32)[0]
        e_phnum = struct.unpack_from("<H", elf_header, 56)[0]
        e_phentsize = struct.unpack_from("<H", elf_header, 54)[0]

        for i in range(e_phnum):
            offset = e_phoff + i * e_phentsize
            f.seek(offset)
            entry = f.read(e_phentsize)
            p_type = struct.unpack_from("<I", entry, 0)[0]
            if p_type == 4:  # PT_NOTE
                filesz = struct.unpack_from("<Q", entry, 32)[0]
                memsz = struct.unpack_from("<Q", entry, 40)[0]

                if filesz <= TARGET_SIZE and memsz <= TARGET_SIZE:
                    return False

                f.seek(offset + 32)
                f.write(struct.pack("<Q", TARGET_SIZE))
                f.seek(offset + 40)
                f.write(struct.pack("<Q", TARGET_SIZE))
                print(f"Patched: filesz={filesz:#x} -> {TARGET_SIZE:#x}, "
                      f"memsz={memsz:#x} -> {TARGET_SIZE:#x}")
                return True

        print("ERROR: PT_NOTE not found", file=sys.stderr)
        return False


if __name__ == "__main__":
    if not BINARY.exists():
        print(f"ERROR: {BINARY} not found", file=sys.stderr)
        sys.exit(1)

    changed = patch_elf_note_segment()
    if changed:
        print("Patch applied.")
    sys.exit(0)
