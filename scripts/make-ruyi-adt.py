#!/usr/bin/env python3
"""Generate the minimal ruyi hardware Apple-format DT for the first boot.

The tree mirrors the structure the source XNU boot converged on, with the SM8650 (Lanai)
values taken from the compiled board DT:

  - GICv3 distributor 0x17100000/0x10000, redistributor 0x17180000/0x200000 under an APSS
    range at 0x17000000, redistributor stride 0x40000
  - CPU timebase 19200000 Hz
  - DRAM base 0x80000000, size from the caller
  - UFS host controller 0x1d84000, PHY 0x1d80000 (below the arm-io window, class match only)

This is first-boot bring-up data. No Apple device identity is claimed unless --ipad-identity
is passed, in which case the tree describes the iPad7,1 identity the image is bound to.
"""
from pathlib import Path
import argparse
import os
import struct
import zlib

ROOT = Path(__file__).resolve().parents[1]

DRAM_BASE = 0x80000000
ARMIO_BASE = 0x17000000
GIC_DIST = 0x17100000
GIC_DIST_SIZE = 0x10000
GIC_REDIST = 0x17180000
GIC_REDIST_SIZE = 0x200000
REDIST_STRIDE = 0x40000
TIMEBASE = 19200000


def node(name, properties=(), children=()):
    properties = [('name', name.encode() + b'\0'), *properties]
    result = struct.pack('<II', len(properties), len(children))
    for key, data in properties:
        result += struct.pack('<32sI', key.encode(), len(data))
        result += data + bytes(-len(data) % 4)
    return result + b''.join(children)


def empty_nvram_proxy():
    """A fresh volatile CHRP bank for upstream IODTNVRAM, with no controller."""
    def header(name, size):
        raw = bytearray(struct.pack('<BBH12s', 0x70, 0, size // 16, name))
        checksum = sum(raw)
        while checksum > 255:
            checksum = (checksum & 255) + (checksum >> 8)
        raw[1] = checksum
        return raw

    image = bytearray(8192)
    image[:16] = header(b'nvram', 32)
    image[32:48] = header(b'common', 16 + 2048)
    free_at = 32 + 16 + 2048
    image[free_at:free_at + 16] = header(b'free', len(image) - free_at)
    struct.pack_into('<I', image, 16, zlib.adler32(image[20:]))
    return [('nvram-bank-size', struct.pack('<I', len(image))),
            ('nvram-bank-count', struct.pack('<I', 1)),
            ('nvram-current-bank', struct.pack('<I', 0)),
            ('nvram-proxy-data', bytes(image))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'build/ruyi/boot/kernel.macho.adt')
    parser.add_argument('--dram-size', type=lambda value: int(value, 0), required=True,
                        help='Actual observed DRAM size, e.g. 0x300000000')
    parser.add_argument('--owned-memory-size', type=lambda value: int(value, 0), default=None,
                        help='DarwinBoot contiguous window request, 256 MiB..8 GiB in 2 MiB steps')
    parser.add_argument('--root-match-volume-uuid', default=None,
                        help='/chosen/root-matching by IOMedia UUID')
    parser.add_argument('--unique-chip-id', type=lambda value: int(value, 16), default=None,
                        help='/chosen/unique-chip-id (8 bytes)')
    parser.add_argument('--chip-id', type=lambda value: int(value, 16), default=None,
                        help='/chosen/chip-id (4 bytes)')
    parser.add_argument('--ipad-identity', action='store_true',
                        help='Describe an iPad-class device without a Home button')
    parser.add_argument('--cpu-regs', default='0',
                        help='Comma-separated MPIDR Affinity1 values, boot CPU first')
    parser.add_argument('--psci-method', choices=['smc', 'hvc'], default='smc')
    parser.add_argument('--packed-data-ramdisk', action='store_true')
    parser.add_argument('--data-ramdisk', action='store_true')
    parser.add_argument('--persistent-data', action='store_true')
    parser.add_argument('--trust-cache', action='store_true')
    parser.add_argument('--gpu-addon', action='store_true')
    parser.add_argument('--text-fixup', action='append', default=[])
    args = parser.parse_args()

    #
    # A zero chip id means "not configured": drop it instead of failing, so the payload can be
    # built before the Apple identity seeds are decided.
    #
    chip_id = args.chip_id or None
    unique_chip_id = args.unique_chip_id or None
    if (unique_chip_id is None) != (chip_id is None):
        if chip_id is not None and unique_chip_id is None:
            unique_chip_id = chip_id
        elif unique_chip_id is not None and chip_id is None:
            chip_id = unique_chip_id & 0xFFFFFFFF

    if (unique_chip_id is None) != (chip_id is None):
        parser.error('--unique-chip-id and --chip-id must be supplied together')
    if unique_chip_id is not None and not (0 < unique_chip_id < 1 << 64 and 0 < chip_id < 1 << 32):
        parser.error('--unique-chip-id must fit 8 bytes and --chip-id 4 bytes, both nonzero')
    if args.owned_memory_size is not None and (
            not 0x10000000 <= args.owned_memory_size <= 0x200000000
            or args.owned_memory_size % 0x200000):
        parser.error('Owned memory must be 256 MiB to 8 GiB, aligned to 2 MiB')

    try:
        cpu_regs = [int(value, 0) for value in args.cpu_regs.split(',')]
    except ValueError:
        parser.error('--cpu-regs must be integers')
    if (not cpu_regs or cpu_regs[0] != 0 or len(set(cpu_regs)) != len(cpu_regs) or len(cpu_regs) > 8
            or any(reg < 0 or reg > 0xffff or reg & 0xff for reg in cpu_regs)):
        parser.error('--cpu-regs: up to eight distinct Affinity1-numbered cores, boot CPU 0 first')

    cpus = [node('cpu%d' % index,
                 [('state', b'running\0' if index == 0 else b'waiting\0'),
                  ('reg', struct.pack('<I', reg)),
                  ('device_type', b'cpu\0'),
                  ('compatible', b'arm,cortex-a715\0'),
                  ('timebase-frequency', struct.pack('<I', TIMEBASE))])
            for index, reg in enumerate(cpu_regs)]

    tree = node('device-tree', [
        ('model', b'iPad7,1\0' if args.ipad_identity else b'ruyi-first-boot\0'),
        *([('target-type', b'J120\0')] if args.ipad_identity else []),
        ('compatible', b'xiaomi,ruyi\0qcom,sm8650\0'),
        ('#address-cells', struct.pack('<I', 2)),
        ('#size-cells', struct.pack('<I', 2)),
    ], [
        node('chosen', [
            ('random-seed', os.urandom(64)),
            *([('unique-chip-id', struct.pack('<Q', unique_chip_id)),
               ('chip-id', struct.pack('<I', chip_id))] if unique_chip_id is not None else []),
            ('osenvironment', b'main\0'),
            *([('certificate-production-status', struct.pack('<I', 0)),
               ('effective-production-status-ap', struct.pack('<I', 0)),
               ('certificate-security-mode', struct.pack('<I', 1)),
               ('effective-security-mode-sep', struct.pack('<I', 1))] if args.ipad_identity else []),
            *([('ephemeral-storage', struct.pack('<I', 1))]
              if (args.data_ramdisk or args.packed_data_ramdisk) and not args.persistent_data else []),
            *([('darwinboot-memory-size', struct.pack('<Q', args.owned_memory_size))]
              if args.owned_memory_size else []),
            ('dram-base', struct.pack('<Q', DRAM_BASE)),
            ('dram-size', struct.pack('<Q', args.dram_size)),
            ('crypto-hash-method', b'sha2-384\0'),
            *([('root-matching', ('<dict><key>IOProviderClass</key><string>IOMedia</string>'
                                  '<key>IOPropertyMatch</key><dict><key>UUID</key><string>' +
                                  args.root_match_volume_uuid.upper() + '</string></dict></dict>\0').encode())]
              if args.root_match_volume_uuid else []),
            ('amfi-only-platform-code', struct.pack('<I', 1)),
            *([('text-fixups', b''.join(f.encode() + b'\0' for f in args.text_fixup))]
              if args.text_fixup else []),
            *empty_nvram_proxy(),
        ], [
            node('memory-map',
                 ([('PackedRAMDisk' if args.packed_data_ramdisk else 'RAMDisk', bytes(16))]
                  if (args.data_ramdisk or args.packed_data_ramdisk) else []) +
                 ([('TrustCache', bytes(16))] if args.trust_cache else []) +
                 ([('GpuAddon', bytes(16))] if args.gpu_addon else [])),
            node('manifest-properties'),
        ]),
        node('cpus', children=cpus),
        *([node('psci', [('compatible', b'arm,psci-1.0\0'),
                         ('method', args.psci_method.encode() + b'\0')])] if len(cpus) > 1 else []),
        node('defaults', [('sleep-disabled', struct.pack('<I', 1)),
                          ('kern.maxvnodes', struct.pack('<I', 66560))]),
        node('options'),
        *([node('buttons', [('compatible', b'buttons\0'), ('device_type', b'buttons\0'),
                            ('button-names', b'hold\0voldown\0volup\0'),
                            ('home-button-type', struct.pack('<I', 2))])] if args.ipad_identity else []),
        node('product', [
            ('product-name', b'iPad Pro (12.9-inch) (2nd generation)\0' if args.ipad_identity
                             else b'Xiaomi MIX Flip\0'),
            ('model', b'xiaomi,ruyi\0'),
            *([('product-description', b'iPad Pro (12.9-inch) (2nd generation)\0'),
               ('artwork-device-idiom', b'pad\0'),
               ('artwork-device-subtype', struct.pack('<I', 2732)),
               ('artwork-scale-factor', struct.pack('<I', 2)),
               ('artwork-display-gamut', b'P3\0'),
               ('compatible-device-fallback', b'iPad6,7\0'),
               ('thin-bezel', struct.pack('<I', 1))] if args.ipad_identity else []),
        ]),
        node('arm-io', [
            ('device_type', b'arm-io\0'),
            ('#address-cells', struct.pack('<I', 2)),
            ('#size-cells', struct.pack('<I', 2)),
            ('ranges', struct.pack('<QQQ', 0, ARMIO_BASE, 0x08000000)),
        ], [
            node('intc', [('compatible', b'arm,gic-v3\0'),
                          ('reg', struct.pack('<4Q',
                                              GIC_DIST - ARMIO_BASE, GIC_DIST_SIZE,
                                              GIC_REDIST - ARMIO_BASE, GIC_REDIST_SIZE)),
                          ('redistributor-stride', struct.pack('<Q', REDIST_STRIDE))]),
            node('ufshc', [('compatible', b'qcom,sm8650-ufshc\0')]),
        ]),
    ])

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(tree)
    print(f'ruyi ADT: {len(tree)} bytes, dram {args.dram_size:#x}, '
          f'cpus {len(cpu_regs)}, identity {"iPad7,1" if args.ipad_identity else "generic"}')


if __name__ == '__main__':
    main()
