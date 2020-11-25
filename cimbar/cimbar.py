#!/usr/bin/python3

"""color-icon-matrix barcode

Usage:
  ./cimbar.py (<src_image> | --src_image=<filename>) (<dst_data> | --dst_data=<filename>) [--dark | --light]
              [--deskew=<0-2>] [--ecc=<0-150>] [--fountain] [--force-preprocess]
  ./cimbar.py --encode (<src_data> | --src_data=<filename>) (<dst_image> | --dst_image=<filename>) [--dark | --light]
                       [--ecc=<0-150>] [--fountain]
  ./cimbar.py (-h | --help)

Examples:
  python -m cimbar --encode myfile.txt cimb-code.png
  python -m cimbar cimb-code.png myfile.txt

Options:
  -h --help                        Show this help.
  --version                        Show version.
  --src_data=<filename>            For encoding. Data to encode.
  --dst_image=<filename>           For encoding. Where to store encoded image.
  --src_image=<filename>           For decoding. Image to try to decode
  --dst_data=<filename>            For decoding. Where to store decoded data.
  -e --ecc=<0-150>                 Reed solomon error correction level. 0 is no ecc. [default: 30]
  -f --fountain                    Use fountain encoding scheme.
  --dark                           Use dark palette. [default]
  --light                          Use light palette.
  --deskew=<0-2>                   Deskew level. 0 is no deskew. Should be 0 or default, except for testing. [default: 2]
  --force-preprocess               Always run sharpening filters on image before decoding.
"""
from os import path
from tempfile import TemporaryDirectory

import cv2
import numpy
from docopt import docopt
from PIL import Image

from cimbar.deskew.deskewer import deskewer
from cimbar.encode.cell_positions import cell_positions, AdjacentCellFinder, FloodDecodeOrder
from cimbar.encode.cimb_translator import CimbEncoder, CimbDecoder
from cimbar.encode.rss import reed_solomon_stream
from cimbar.fountain.fountain_decoder_stream import fountain_decoder_stream
from cimbar.fountain.fountain_encoder_stream import fountain_encoder_stream
from cimbar.util.bit_file import bit_file
from cimbar.util.interleave import interleave, interleave_reverse, interleaved_writer


TOTAL_SIZE = 1024
BITS_PER_SYMBOL = 4
BITS_PER_COLOR = 2
BITS_PER_OP = BITS_PER_SYMBOL + BITS_PER_COLOR
CELL_SIZE = 8
CELL_SPACING = CELL_SIZE + 1
CELL_DIMENSIONS = 112
CELLS_OFFSET = 8
ECC = 30
INTERLEAVE_BLOCKS = 155
INTERLEAVE_PARTITIONS = 2
FOUNTAIN_BLOCKS = 10


def get_deskew_params(level):
    level = int(level)
    return {
        'deskew': level,
        'auto_dewarp': level >= 2,
    }


def _fountain_chunk_size(ecc=ECC, bits_per_op=BITS_PER_OP, fountain_blocks=FOUNTAIN_BLOCKS):
    return int((155-ecc) * bits_per_op * 10 / fountain_blocks)


def detect_and_deskew(src_image, temp_image, dark, auto_dewarp=True):
    return deskewer(src_image, temp_image, dark, auto_dewarp=auto_dewarp)


def _decode_cell(ct, img, color_img, x, y, drift):
    best_distance = 1000
    for dx, dy in drift.pairs:
        testX = x + drift.x + dx
        testY = y + drift.y + dy
        img_cell = img.crop((testX, testY, testX + CELL_SIZE, testY + CELL_SIZE))
        bits, min_distance = ct.decode_symbol(img_cell)
        best_distance = min(min_distance, best_distance)
        if min_distance == best_distance:
            best_bits = bits
            best_dx = dx
            best_dy = dy
        if min_distance < 8:
            break

    testX = x + drift.x + best_dx
    testY = y + drift.y + best_dy
    best_cell = color_img.crop((testX+1, testY+1, testX + CELL_SIZE-2, testY + CELL_SIZE-2))
    return best_bits + ct.decode_color(best_cell), best_dx, best_dy, best_distance


def _preprocess_for_decode(img):
    ''' This might need to be conditional based on source image size.'''
    img = cv2.cvtColor(numpy.array(img), cv2.COLOR_RGB2BGR)
    kernel = numpy.array([[-1.0,-1.0,-1.0], [-1.0,8.5,-1.0], [-1.0,-1.0,-1.0]])
    img = cv2.filter2D(img, -1, kernel)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    img = Image.fromarray(img)
    return img


def decode_iter(src_image, dark, force_preprocess, deskew, auto_dewarp):
    should_preprocess = force_preprocess
    tempdir = None
    if deskew:
        tempdir = TemporaryDirectory()
        temp_img = path.join(tempdir.name, path.basename(src_image))
        dims = detect_and_deskew(src_image, temp_img, dark, auto_dewarp)
        should_preprocess |= dims[0] < TOTAL_SIZE or dims[1] < TOTAL_SIZE
        color_img = Image.open(temp_img)
    else:
        color_img = Image.open(src_image)
    ct = CimbDecoder(dark, symbol_bits=BITS_PER_SYMBOL, color_bits=BITS_PER_COLOR)
    img = _preprocess_for_decode(color_img) if should_preprocess else color_img

    cell_pos = cell_positions(CELL_SPACING, CELL_DIMENSIONS, CELLS_OFFSET)
    finder = AdjacentCellFinder(cell_pos, CELL_DIMENSIONS)
    decode_order = FloodDecodeOrder(cell_pos, finder)
    for i, (x, y), drift in decode_order:
        best_bits, best_dx, best_dy, best_distance = _decode_cell(ct, img, color_img, x, y, drift)
        decode_order.update(best_dx, best_dy, best_distance)
        yield i, best_bits

    if tempdir:  # cleanup
        with tempdir:
            pass


def decode(src_image, outfile, dark=False, ecc=ECC, fountain=False, force_preprocess=False, deskew=True, auto_dewarp=True):
    cells = cell_positions(CELL_SPACING, CELL_DIMENSIONS, CELLS_OFFSET)
    interleave_lookup, block_size = interleave_reverse(cells, INTERLEAVE_BLOCKS, INTERLEAVE_PARTITIONS)

    # set up the outstream: image -> reedsolomon -> fountain -> zstd_decompress -> raw bytes
    fds = fountain_decoder_stream(outfile, _fountain_chunk_size(ecc)) if fountain else open(outfile, 'wb')
    rss = reed_solomon_stream(fds, ecc, mode='write') if ecc else fds

    with rss as outstream, interleaved_writer(f=outstream, bits_per_op=BITS_PER_OP, mode='write') as iw:
        decoding = {i: bits for i, bits in decode_iter(src_image, dark, force_preprocess, deskew, auto_dewarp)}
        for i, bits in sorted(decoding.items()):
            block = interleave_lookup[i] // block_size
            iw.write(bits, block)


def _get_image_template(width, dark):
    color = (0, 0, 0) if dark else (255, 255, 255)
    img = Image.new('RGB', (width, width), color=color)

    suffix = 'dark' if dark else 'light'
    anchor = Image.open(f'bitmap/anchor-{suffix}.png')
    anchor_br = Image.open(f'bitmap/anchor-secondary-{suffix}.png')
    aw, ah = anchor.size
    img.paste(anchor, (0, 0))
    img.paste(anchor, (0, width-ah))
    img.paste(anchor, (width-aw, 0))
    img.paste(anchor_br, (width-aw, width-ah))

    horizontal_guide = Image.open(f'bitmap/guide-horizontal-{suffix}.png')
    gw, _ = horizontal_guide.size
    img.paste(horizontal_guide, (width//2 - gw//2, 2))
    img.paste(horizontal_guide, (width//2 - gw//2, width-4))
    img.paste(horizontal_guide, (width//2 - gw - gw//2, width-4))  # long bottom guide
    img.paste(horizontal_guide, (width//2 + gw - gw//2, width-4))  # ''

    vertical_guide = Image.open(f'bitmap/guide-vertical-{suffix}.png')
    _, gh = vertical_guide.size
    img.paste(vertical_guide, (2, width//2 - gw//2))
    img.paste(vertical_guide, (width-4, width//2 - gw//2))
    return img


def encode_iter(src_data, ecc, fountain):
    # various checks to set up the instream.
    # the hierarchy is raw bytes -> zstd -> fountain -> reedsolomon -> image
    fes = fountain_encoder_stream(src_data, _fountain_chunk_size(ecc)) if fountain else open(src_data, 'rb')
    rss = reed_solomon_stream(fes, ecc) if ecc else fes
    read_size = _fountain_chunk_size(ecc) if fountain else 16384

    with rss as instream, bit_file(instream, bits_per_op=BITS_PER_OP, read_size=read_size) as f:
        cells = cell_positions(CELL_SPACING, CELL_DIMENSIONS, CELLS_OFFSET)
        for x, y in interleave(cells, INTERLEAVE_BLOCKS, INTERLEAVE_PARTITIONS):
            bits = f.read()
            yield bits, x, y


def encode(src_data, dst_image, dark=False, ecc=ECC, fountain=False):
    img = _get_image_template(TOTAL_SIZE, dark)
    ct = CimbEncoder(dark, symbol_bits=BITS_PER_SYMBOL, color_bits=BITS_PER_COLOR)
    for bits, x, y in encode_iter(src_data, ecc, fountain):
        encoded = ct.encode(bits)
        img.paste(encoded, (x, y))
    img.save(dst_image)


def main():
    args = docopt(__doc__, version='cimbar 0.0.2')

    dark = args['--dark'] or not args['--light']
    ecc = int(args.get('--ecc'))
    fountain = bool(args.get('--fountain'))

    if args['--encode']:
        src_data = args['<src_data>'] or args['--src_data']
        dst_image = args['<dst_image>'] or args['--dst_image']
        encode(src_data, dst_image, dark, ecc, fountain)
        return

    deskew = get_deskew_params(args.get('--deskew'))
    force_preprocess = args.get('--force-preprocess')
    src_image = args['<src_image>'] or args['--src_image']
    dst_data = args['<dst_data>'] or args['--dst_data']
    decode(src_image, dst_data, dark, ecc, fountain, force_preprocess, **deskew)


if __name__ == '__main__':
    main()
