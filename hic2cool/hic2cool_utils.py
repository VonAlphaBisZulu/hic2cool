"""
hic2cool
--------

CLI for a converter between .hic files (from juicer) and .cool files (for
cooler).  Both hic and cool files describe Hi-C contact matrices. Intended to be
lightweight, this is a stand-alone Python file written by Carl Vitzthum of the
HMS DBMI Park lab. Originally published 1/26/17.

This code was originally based off of the straw project by Neva C. Durand and
Yue Wu (https://github.com/theaidenlab/straw). The cooler file writing was based
off of much of the CLI code contained in this repo:
https://github.com/mirnylab/cooler.

See README for more information
"""
from __future__ import (
    absolute_import,
    division,
    print_function,
    unicode_literals
)
import sys
import time
import struct
import zlib
import math
import copy
import mmap
import os
import shutil
import cooler
import h5py
import numpy as np
import pandas as pd
from datetime import datetime
from collections import OrderedDict
from ._version import __version__
from .hic2cool_updates import prepare_hic2cool_updates
from .hic2cool_config import *
from multiprocess import Pool

# input vs. raw_input for python 2/3
try:
    use_input = raw_input
except NameError:
    use_input = input


# read function
def readcstr(f):
    # buf = bytearray()
    buf = b""
    while True:
        b = f.read(1)
        if b is None or b == b"\0":
            # return buf.encode("utf-8", errors="ignore")
            return buf.decode("utf-8")
        elif b == "":
            raise EOFError("Buffer unexpectedly empty while trying to read null-terminated string")
        else:
            buf += b

def read_header(req):
    """
    Takes in a .hic file and returns a dictionary containing information about
    the chromosome. Keys are chromosome index numbers (0 through # of chroms
    contained in file) and values are [chr idx (int), chr name (str), chrom
    length (str)]. Returns the masterindex used by the file as well as the open
    file object.

    """
    chrs = {}
    resolutions = []
    magic_string = struct.unpack(b'<3s', req.read(3))[0]
    req.read(1)
    if magic_string != b"HIC":
        error_string = '... This does not appear to be a HiC file; magic string is incorrect'
        force_exit(error_string, req)
    global version
    version = struct.unpack(b'<i', req.read(4))[0]
    footerPosition = struct.unpack(b'<q', req.read(8))[0]
    genome = b""
    c = req.read(1)
    while c != b'\0':
        genome += c
        c = req.read(1)
    genome = genome.decode('ascii')
    if version >= 9:
        frag_resolutions = []
        normVectorIndexPosition = struct.unpack('<q', req.read(8))[0]
        normVectorIndexLength = struct.unpack('<q', req.read(8))[0]
    nattributes = struct.unpack(b'<i', req.read(4))[0]
    metadata = {}
    for _ in range(nattributes):
        key = readcstr(req)
        value = readcstr(req)
        metadata[key] = value

    nChrs = struct.unpack(b'<i', req.read(4))[0]
    for i in range(nChrs):
        name = readcstr(req)
        if version >= 9:
            length = struct.unpack(b'<q', req.read(8))[0]
        else:
            length = struct.unpack(b'<i', req.read(4))[0]
        if name and length:
            chrs[i] = [i, name, length]

    nBpRes = struct.unpack(b'<i', req.read(4))[0]
    for _ in range(nBpRes):
        res = struct.unpack(b'<i', req.read(4))[0]
        resolutions.append(res)
        
    nBpResFrag = struct.unpack(b'<i', req.read(4))[0]
    for _ in range(nBpResFrag):
        res = struct.unpack(b'<i', req.read(4))[0]
        frag_resolutions.append(res)

    return chrs, resolutions, footerPosition, genome, metadata

def read_footer(f, buf, footerPosition):
    f.seek(footerPosition)

    cpair_info = {}
    if version >= 9:
        nBytes = struct.unpack(b'<q', f.read(8))[0]
    else:
        nBytes = struct.unpack(b'<i', f.read(4))[0]
    nEntries = struct.unpack(b'<i', f.read(4))[0]
    for _ in range(nEntries):
        key = readcstr(f)
        fpos = struct.unpack(b'<q', f.read(8))[0]
        sizeinbytes = struct.unpack(b'<i', f.read(4))[0]
        cpair_info[key] = fpos

    expected = {}
    factors = {}
    norm_info = {}
    # raw (norm == 'NONE')
    nExpectedValues = struct.unpack(b'<i', f.read(4))[0]
    for _ in range(nExpectedValues):
        unit = readcstr(f)
        binsize = struct.unpack(b'<i', f.read(4))[0]
        if version >= 9:
            nValues = struct.unpack(b'<q', f.read(8))[0]
            expected['RAW', unit, binsize] = np.frombuffer(
                buf,
                dtype='<f',
                count=nValues,
                offset=f.tell())
            f.seek(nValues * 4, 1)
            nNormalizationFactors = struct.unpack(b'<i', f.read(4))[0]
            factors['RAW', unit, binsize] = np.frombuffer(
                buf,
                dtype={'names':['chrom','factor'], 'formats':['<i', '<f']},
                count=nNormalizationFactors,
                offset=f.tell())
            f.seek(nNormalizationFactors * 8, 1)
        else:
            nValues = struct.unpack(b'<i', f.read(4))[0]
            expected['RAW', unit, binsize] = np.frombuffer(
                buf,
                dtype=np.dtype('<d'),
                count=nValues,
                offset=f.tell())
            f.seek(nValues * 8, 1)
            nNormalizationFactors = struct.unpack(b'<i', f.read(4))[0]
            factors['RAW', unit, binsize] = np.frombuffer(
                buf,
                dtype={'names':['chrom','factor'], 'formats':['<i', '<d']},
                count=nNormalizationFactors,
                offset=f.tell())
            f.seek(nNormalizationFactors * 12, 1)
            
    # normalized (norm != 'NONE')
    possibleNorms = f.read(4)
    if not possibleNorms:
        print_stderr('!!! WARNING. No normalization vectors found in the hic file.')
        return cpair_info, expected, factors, norm_info
    
    
    nExpectedValues = struct.unpack(b'<i', possibleNorms)[0]
    for _ in range(nExpectedValues):
        normtype = readcstr(f)
        if normtype not in NORMS:
            NORMS.append(normtype)
        unit = readcstr(f)
        binsize = struct.unpack(b'<i', f.read(4))[0]
        if version >= 9:
            nValues = struct.unpack(b'<q', f.read(8))[0]
            expected[normtype, unit, binsize] = np.frombuffer(
                buf,
                dtype='<f',
                count=nValues,
                offset=f.tell())
            f.seek(nValues * 4, 1)
            nNormalizationFactors = struct.unpack(b'<i', f.read(4))[0]
            factors[normtype, unit, binsize] = np.frombuffer(
                buf,
                dtype={'names':['chrom','factor'], 'formats':['<i', '<f']},
                count=nNormalizationFactors,
                offset=f.tell())
            f.seek(nNormalizationFactors * 8, 1)
        else:
            nValues = struct.unpack(b'<i', f.read(4))[0]
            expected['RAW', unit, binsize] = np.frombuffer(
                buf,
                dtype=np.dtype('<d'),
                count=nValues,
                offset=f.tell())
            f.seek(nValues * 8, 1)
            nNormalizationFactors = struct.unpack(b'<i', f.read(4))[0]
            factors['RAW', unit, binsize] = np.frombuffer(
                buf,
                dtype={'names':['chrom','factor'], 'formats':['<i', '<d']},
                count=nNormalizationFactors,
                offset=f.tell())
            f.seek(nNormalizationFactors * 12, 1)

    nEntries = struct.unpack(b'<i', f.read(4))[0]
    for _ in range(nEntries):
        normtype = readcstr(f)
        chrIdx = struct.unpack(b'<i', f.read(4))[0]
        unit = readcstr(f)
        resolution = struct.unpack(b'<i', f.read(4))[0]
        filePosition = struct.unpack(b'<q', f.read(8))[0]
        if version >= 9:
            sizeInBytes = struct.unpack(b'<q', f.read(8))[0]
        else:
            sizeInBytes = struct.unpack(b'<i', f.read(4))[0]
        norm_info[normtype, unit, resolution, chrIdx] = {
            'filepos': filePosition,
            'size': sizeInBytes
        }

    return cpair_info, expected, factors, norm_info

def read_blockinfo(f, buf, filepos, unit, binsize):
    """
    Read the locations of the data blocks of a cpair
    """
    f.seek(filepos)

    c1 = struct.unpack('<i', f.read(4))[0]
    c2 = struct.unpack('<i', f.read(4))[0]
    nRes = struct.unpack('<i', f.read(4))[0]
    for _ in range(nRes):
        unit_ = readcstr(f)
        f.seek(5 * 4, 1)
        binsize_ = struct.unpack('<i', f.read(4))[0]
        blockSize = struct.unpack('<i', f.read(4))[0]
        blockColCount = struct.unpack('<i', f.read(4))[0]
        nBlocks = struct.unpack('<i', f.read(4))[0]
        if unit == unit_ and binsize == binsize_:
            break
        f.seek(nBlocks * 16, 1)

    dt = {'names': ['block_id','filepos','size'], 'formats': ['<i', '<q', '<i']}
    data = np.frombuffer(buf, dt, count=nBlocks, offset=f.tell())
    return data, blockSize, blockColCount


def read_block(req, block_record):
    """
    Uses a block_record built by read_blockinfo to find records of
    chrX/chrY/counts for a given block
    """
    if len(block_record) == 3:
        block_idx = block_record[0]
        position = block_record[1]
        size = block_record[2]
    else: # malformed block
        return np.zeros(0, dtype=CHUNK_DTYPE)
    req.seek(position)
    compressedBytes = req.read(size)
    uncompressedBytes = zlib.decompress(compressedBytes)
    nRecords = struct.unpack(b'<i', uncompressedBytes[0:4])[0]
    block = np.zeros(nRecords, dtype=CHUNK_DTYPE)
    binX = block['bin1_id']
    binY = block['bin2_id']
    counts = block['count']
    if (version < 7):
        for i in range(nRecords):
            x = struct.unpack(b'<i', uncompressedBytes[(12*i+4):(12*i+8)])[0]
            y = struct.unpack(b'<i', uncompressedBytes[(12*i+8):(12*i+12)])[0]
            c = struct.unpack(b'<f', uncompressedBytes[(12*i+12):(12*i+16)])[0]
            binX[i] = x
            binY[i] = y
            counts[i] = c
    elif (version >= 9):
        binXOffset = struct.unpack(b'<i', uncompressedBytes[4:8])[0]
        binYOffset = struct.unpack(b'<i', uncompressedBytes[8:12])[0]
        useFloatContact = struct.unpack(b'<b', uncompressedBytes[12:13])[0]
        useIntXPos = struct.unpack(b'<b', uncompressedBytes[13:14])[0]
        useIntYPos = struct.unpack(b'<b', uncompressedBytes[14:15])[0]
        type_ = struct.unpack(b'<b', uncompressedBytes[15:16])[0]
        k = 0
        if (type_ == 1):
            # Get number of rows
            if (useIntYPos==0):
                rowCount = struct.unpack(b'<h', uncompressedBytes[16:18])[0]
                temp = 18
            else:
                rowCount = struct.unpack(b'<i', uncompressedBytes[16:20])[0]
                temp = 20
            # Iterate throught rows
            for i in range(rowCount):
                # Get row number
                if (useIntYPos==0):
                    rowNumber = struct.unpack(b'<h', uncompressedBytes[temp:(temp+2)])[0]
                    temp += 2
                    rowNumber += binYOffset
                else:
                    rowNumber = struct.unpack(b'<i', uncompressedBytes[temp:(temp+4)])[0]
                    temp += 4
                    rowNumber += binYOffset
                # Get column numbers
                if (useIntXPos==0):
                    recordCount = struct.unpack(b'<h', uncompressedBytes[temp:(temp+2)])[0]
                    temp += 2
                else:
                    recordCount = struct.unpack(b'<i', uncompressedBytes[temp:(temp+4)])[0]
                    temp += 4
                # Iterate throught columns of specific row
                for _ in range(recordCount):
                    if (useIntXPos==0):
                        binColumn = struct.unpack(b'<h', uncompressedBytes[temp:(temp+2)])[0]
                        binColumn += binXOffset
                        temp += 2
                    else:
                        binColumn = struct.unpack(b'<i', uncompressedBytes[temp:(temp+4)])[0]
                        binColumn += binXOffset
                        temp += 4
                    if (useFloatContact==0):
                        c = struct.unpack(b'<h', uncompressedBytes[temp:(temp+2)])[0]
                        temp += 2
                    else:
                        c = struct.unpack(b'<f', uncompressedBytes[temp:(temp+4)])[0]
                        temp += 4
                    binX[k] = binColumn
                    binY[k] = rowNumber
                    counts[k] = c
                    k += 1
        elif (type_== 2):
            temp = 14
            nPts = struct.unpack(b'<i', uncompressedBytes[temp:(temp+4)])[0]
            temp += 4
            w = struct.unpack(b'<h', uncompressedBytes[temp:(temp+2)])[0]
            temp += 2
            for i in range(nPts):
                row = int(i / w)
                col = i - row * w
                x = int(binXOffset + col)
                y = int(binYOffset + row)
                if useFloatContact == 0:
                    c = struct.unpack(b'<h', uncompressedBytes[temp:(temp+2)])[0]
                    temp = temp+2
                    if c != -32768:
                        binX[k] = x
                        binY[k] = y
                        counts[k] = c
                        k += 1
                else:
                    c = struct.unpack(b'<f',uncompressedBytes[temp:(temp+4)])[0]
                    temp=temp+4
                    if c != 0x7fc00000 and not math.isnan(c):
                        binX[k] = x
                        binY[k] = y
                        counts[k] = c
                        k += 1
    else:
        binXOffset = struct.unpack(b'<i', uncompressedBytes[4:8])[0]
        binYOffset = struct.unpack(b'<i', uncompressedBytes[8:12])[0]
        useShort = struct.unpack(b'<b', uncompressedBytes[12:13])[0]
        type_ = struct.unpack(b'<b', uncompressedBytes[13:14])[0]
        k = 0
        if (type_ == 1):
            rowCount = struct.unpack(b'<h', uncompressedBytes[14:16])[0]
            temp = 16
            for i in range(rowCount):
                y = struct.unpack(b'<h', uncompressedBytes[temp:(temp+2)])[0]
                temp = temp+2
                y += binYOffset
                colCount = struct.unpack(b'<h', uncompressedBytes[temp:(temp+2)])[0]
                temp = temp+2
                for j in range(colCount):
                    x = struct.unpack(b'<h', uncompressedBytes[temp:(temp+2)])[0]
                    temp = temp+2
                    x += binXOffset
                    if (useShort==0):
                        c = struct.unpack(b'<h', uncompressedBytes[temp:(temp+2)])[0]
                        temp = temp+2
                    else:
                        c = struct.unpack(b'<f', uncompressedBytes[temp:(temp+4)])[0]
                        temp = temp+4
                    binX[k] = x
                    binY[k] = y
                    counts[k] = c
                    k += 1
        elif (type_== 2):
            temp = 14
            nPts = struct.unpack(b'<i', uncompressedBytes[temp:(temp+4)])[0]
            temp = temp+4
            w = struct.unpack(b'<h', uncompressedBytes[temp:(temp+2)])[0]
            temp = temp+2
            for i in range(nPts):
                row = int(i / w)
                col = i - row * w
                x = int(binXOffset + col)
                y = int(binYOffset + row)
                if useShort == 0:
                    c = struct.unpack(b'<h', uncompressedBytes[temp:(temp+2)])[0]
                    temp = temp+2
                    if c != -32768:
                        binX[k] = x
                        binY[k] = y
                        counts[k] = c
                        k += 1
                else:
                    c = struct.unpack(b'<f',uncompressedBytes[temp:(temp+4)])[0]
                    temp=temp+4
                    if c != 0x7fc00000 and not math.isnan(c):
                        binX[k] = x
                        binY[k] = y
                        counts[k] = c
                        k += 1
    del uncompressedBytes
    return block


def read_normalization_vector(f, buf, entry):
    filepos = entry['filepos']
    f.seek(filepos)
    if version >= 9:
        nValues = struct.unpack(b'<q', f.read(8))[0]
        return np.frombuffer(buf, dtype=np.dtype('<f'), count=nValues, offset=filepos+8)
    else:
        nValues = struct.unpack(b'<i', f.read(4))[0]
        return np.frombuffer(buf, dtype=np.dtype('<d'), count=nValues, offset=filepos+4)


def parse_hic(infile, pool, nproc, chr_key, unit, binsize, pair_footer_info,
              chr_offset_map, chr_bins, used_chrs, show_warnings, mmap_buf, req_ar):
    """
    Adapted from the straw() function in the original straw package.
    Mainly, since all chroms are iterated over, the read_header and read_footer
    functions were placed outside of straw() and made to be reusable across
    any chromosome pair.
    As of version 0.4.0, this had a very large re-write based of code
    generously provided by Nezar. Reads are buffered using mmap and a lot of
    functions were condensed
    """
    # join chunk is an np array
    # that will contain the entire list of c1/c2 counts
    join_chunk = np.zeros(shape=0, dtype=CHUNK_DTYPE)
    if unit not in ["BP", "FRAG"]:
        error_string = "!!! ERROR. Unit specified incorrectly, must be one of <BP/FRAG>"
        force_exit(error_string)
    c1 = int(chr_key.split('_')[0])
    c2 = int(chr_key.split('_')[1])
    try:
        pair_footer_info[chr_key]
    except KeyError:
        WARN = True
        if show_warnings:
            c1_name = used_chrs[c1][1]
            c2_name = used_chrs[c2][1]
            print_stderr('... The intersection between %s and %s cannot be found in the hic file.'
                         % (c1_name, c2_name))
        return join_chunk
    region_indices = [0, chr_bins[c1], 0, chr_bins[c2]]
    myFilePos = pair_footer_info[chr_key]
    with open(infile, 'rb') as req:
        block_info, block_size, block_cols = read_blockinfo(req, mmap_buf, myFilePos, unit, binsize)
    if len(block_info) >= nproc and nproc >= 2:
        mpi_result = [None] * nproc
        blocklen = len(block_info) // nproc
        for mpi in range(0, nproc):
            block_start = mpi * blocklen
            if mpi == nproc - 1:
                block_end = len(block_info)
            else:
                block_end = (mpi + 1) * blocklen
            mpi_result[mpi] = pool.apply_async(build_counts_chunk, (mpi, c1, c2, block_info[block_start:block_end], chr_offset_map, region_indices, req_ar))
        result_all = []
        for mpi in range(0, nproc):
            mpi_result[mpi].wait()
            result_all.extend(mpi_result[mpi].get())
        return np.concatenate(result_all, axis=0)
    else:
        result_all = build_counts_chunk(0, c1, c2, block_info, chr_offset_map, region_indices, req_ar)
        return np.concatenate(result_all, axis=0)
    #return build_counts_chunk(nproc, c1, c2, block_info, chr_offset_map, region_indices)


def build_counts_chunk(mpi, c1, c2, block_info, chr_offset_map, region_indices, reqarr):
    """
    Takes the given block_info and find those bin_IDs/counts from the hic file,
    using them to build and return a chunk of counts
    """
    [binX0, binX1, binY0, binY1] = region_indices
    block_results = []
    req_file = reqarr[mpi]
    for block_record in block_info:
        records = read_block(req_file, block_record)
        # skip empty records
        if not records.size:
            continue
            #result[str(mpi)] = np.zeros(0, dtype=CHUNK_DTYPE)
        mask = ((records['bin1_id'] >= binX0) &
                (records['bin1_id'] < binX1) &
                (records['bin2_id'] >= binY0) &
                (records['bin2_id'] < binY1))
        records = records[mask]
        # add chr offsets
        records['bin1_id'] += chr_offset_map[c1]
        records['bin2_id'] += chr_offset_map[c2]
        block_results.append(records)
    return block_results


def initialize_res(outfile, infile, buf, unit, chr_info, genome, metadata,
                   resolution, norm_info, norm, multi_res, show_warnings):
    """
    Use various information to initialize a cooler file in HDF5 format. Tables
    included are chroms, bins, pixels, and indexes.
    For an in-depth explanation of the data structure, please see:
    http://cooler.readthedocs.io/en/latest/datamodel.html

    ONGOING: Keep schema attributes and SCHEMA_VERSION up-to-date with:
    https://cooler.readthedocs.io/en/latest/schema.html
    """
    with h5py.File(outfile, "a") as h5file:
        # if multi_res, organize resolutions as individual cooler files
        # as groups under resolutions. so, a res of 5kb would be
        # ['resolutions/5000']. If single res, put all datasets at top level.
        if multi_res:
            if 'resolutions' in h5file:
                h5res_grp = h5file['resolutions']
            else:
                # Add "/" level mcool attributes and initialize the resolutions
                mcool_info = {
                    'format': MCOOL_FORMAT,
                    'format-version': MCOOL_FORMAT_VERSION
                }
                h5file.attrs.update(mcool_info)
                h5res_grp = h5file.create_group('resolutions')

            h5resolution = h5res_grp.create_group(str(resolution))
        else:
            h5resolution = h5file
        # chroms
        grp = h5resolution.create_group('chroms')
        chr_names = write_chroms(grp, chr_info)
        # bins
        bin_table, chr_offsets, by_chr_bins, by_chr_offset_map = create_bins(chr_info, resolution)
        grp = h5resolution.create_group('bins')
        write_bins(grp, infile, buf, unit, resolution, chr_names, bin_table, by_chr_bins, norm_info, norm, show_warnings)
        n_bins = len(bin_table)
        # indexes (just initialize bin1offets)
        grp = h5resolution.create_group('indexes')
        write_chrom_offset(grp, chr_offsets)
        # pixels (written later)
        grp = h5resolution.create_group('pixels')
        initialize_pixels(grp, n_bins)
        # metadata required by cooler schema
        # w.r.t. metadata, each resolution is considered a full file
        # these should be in unicode
        info = copy.deepcopy(metadata) # initialize with metadata from header
        info['nchroms'] = len(chr_names)
        info['nbins'] = n_bins
        info['bin-type'] = 'fixed'
        info['bin-size'] = resolution
        info['format'] = COOLER_FORMAT
        info['format-url'] = URL
        info['format-version'] = COOLER_FORMAT_VERSION
        info['storage-mode'] = 'symmetric-upper'
        info['generated-by'] = 'hic2cool-' + __version__
        info['genome-assembly'] = genome
        info['creation-date'] = get_unicode_utcnow()
        h5resolution.attrs.update(info)
    return by_chr_offset_map, by_chr_bins


def write_chroms(grp, chrs):
    """
    Write the chroms table, which includes chromosome names and length
    """
    # format chr_names and chr_lengths np arrays
    chr_names = np.array(
        [val[1] for val in chrs.values() if val[1].lower() != 'all'],
        dtype=CHROM_DTYPE)
    chr_lengths = np.array(
        [val[2] for val in chrs.values() if val[1].lower()  != 'all'])
    grp.create_dataset(
        'name',
        shape=(len(chr_names),),
        dtype=CHROM_DTYPE,
        data=chr_names,
        **H5OPTS)
    grp.create_dataset(
        'length',
        shape=(len(chr_lengths),),
        dtype=CHROMSIZE_DTYPE,
        data=chr_lengths,
        **H5OPTS)
    return chr_names


def create_bins(chrs, binsize):
    """
    Cooler requires a bin file with each line having <chrId, binStart, binEnd>
    where chrId is an enum and binStart and binEnd are bp locations.
    Use the chromosome info from the header along with given binsize to build
    this table in numpy. Returns the table (with dimensions #bins x 3) and chr
    offset information with regard to bins
    """
    bins_array = []
    offsets = [0]
    by_chr_offsets = {}
    by_chr_bins = {}
    for c_idx in chrs:
        chr_name = chrs[c_idx][1]
        if chr_name.lower()  == 'all':
            continue
        chr_size = chrs[c_idx][2]
        bin_start = 0
        chr_bin_count = 0
        while bin_start < chr_size:
            bin_end = bin_start + binsize
            if bin_end > chr_size:
                bin_end = chr_size
            bins_array.append([chr_name, bin_start, bin_end])
            chr_bin_count += 1
            bin_start = bin_end
        # offsets are the number of bins of all chromosomes prior to this one
        # by_chr_offsets are bin offsets for each chromosome, indexed by chr_idx
        by_chr_offsets[chrs[c_idx][0]] = offsets[-1]
        by_chr_bins[c_idx] = chr_bin_count
        if len(offsets) < len(chrs):
            offsets.append(int(math.ceil(chr_size/binsize) + offsets[-1]))
    return np.array(bins_array), np.array(offsets), by_chr_bins, by_chr_offsets


def write_bins(grp, infile, buf, unit, res, chroms, bins, by_chr_bins, norm_info, norm_weight, show_warnings):
    """
    Write the bins table, which has columns: chrom, start (in bp), end (in bp),
    and one column for each normalization type, named for the norm type.
    'weight' is a reserved column for `cooler balance`.
    Chrom is an index which corresponds to the chroms table.
    Also write a dataset for each normalization vector within the hic file.
    """
    n_chroms = len(chroms)
    n_bins = len(bins)
    idmap = dict(zip(chroms, range(n_chroms)))
    # Convert chrom names to enum
    chrom_ids = [idmap[chrom.encode('UTF-8')] for chrom in bins[:, 0]]
    starts = [int(val) for val in bins[:, 1]]
    ends = [int(val) for val in bins[:, 2]]
    enum_dtype = h5py.special_dtype(enum=(CHROMID_DTYPE, idmap))
    # Store bins
    grp.create_dataset('chrom',
                       shape=(n_bins,),
                       dtype=enum_dtype,
                       data=chrom_ids,
                       **H5OPTS)
    grp.create_dataset('start',
                       shape=(n_bins,),
                       dtype=COORD_DTYPE,
                       data=starts,
                       **H5OPTS)
    grp.create_dataset('end',
                       shape=(n_bins,),
                       dtype=COORD_DTYPE,
                       data=ends,
                       **H5OPTS)
    # check if user selected normalization factors and if they are available for the desired resolution
    if norm_weight:
        if norm_weight not in NORMS:
            error_str = (
                '!!! ERROR.Normalization method %s does not exist in hic file. Please select one of the following: %s'
                    % (norm_weight, NORMS))
            force_exit(error_str)
        norm_data = []
        for chr_idx in by_chr_bins:
            chr_bin_end = by_chr_bins[chr_idx]
            try:
                # norm_keys = {k:v for k, v in norm_info.items() if k[0] == norm_weight and k[2] == res}
                norm_key = norm_info[norm_weight, unit, res, chr_idx]
                with open(infile, "rb") as req:
                    norm_vector = read_normalization_vector(req, buf, norm_key)
                norm_data.extend(norm_vector[:chr_bin_end])
            except KeyError:
                WARN = True
                if show_warnings:
                    print_stderr('!!! WARNING. Normalization vector %s does not exist for chr idx %s.'
                        % (norm_weight, chr_idx))
                norm_data.extend([np.nan]*chr_bin_end)
        if len(norm_data) != n_bins:
            error_str = (
                '!!! ERROR. Length of normalization vector %s does not match the'
                ' number of bins.\nThis is likely a problem with the hic file' % (norm_weight))
            force_exit(error_str)
        norm_data = [1 if x==0 or np.isnan(x) else 1/x for x in norm_data]
        grp.create_dataset(
            'weight',
            shape=(len(norm_data),),
            dtype=NORM_DTYPE,
            data=np.array(norm_data, dtype=NORM_DTYPE),
                **H5OPTS)
    # write columns for normalization vectors
    for norm in NORMS:
        norm_data = []
        for chr_idx in by_chr_bins:
            chr_bin_end = by_chr_bins[chr_idx]
            try:
                norm_key = norm_info[norm, unit, res, chr_idx]
            except KeyError:
                WARN = True
                if show_warnings:
                    print_stderr('!!! WARNING. Normalization vector %s does not exist for chr idx %s.'
                        % (norm, chr_idx))
                # add a vector of nan's for missing vectors
                norm_data.extend([np.nan]*chr_bin_end)
                continue
            with open(infile, "rb") as req:    
                norm_vector = read_normalization_vector(req, buf, norm_key)
            # NOTE: possible issue to look into
            # norm_vector returned by read_normalization_vector has an extra
            # entry at the end (0.0) that is never used and causes the
            # norm vectors to be longer than n_bins for a given chr
            # restrict the length of norm_vector to chr_bin_end for now.
            norm_data.extend(norm_vector[:chr_bin_end])
            # we are no longer performing the inversion of hic weights, i.e.
            # from .hic2cool_updates import norm_convert
            # norm_data.extend(list(map(norm_convert, norm_vector[:chr_bin_end])))
        if len(norm_data) != n_bins:
            error_str = (
                '!!! ERROR. Length of normalization vector %s does not match the'
                ' number of bins.\nThis is likely a problem with the hic file' % (norm))
            force_exit(error_str)
        grp.create_dataset(
            norm,
            shape=(len(norm_data),),
            dtype=NORM_DTYPE,
            data=np.array(norm_data, dtype=NORM_DTYPE),
            **H5OPTS)


def write_chrom_offset(grp, chr_offsets):
    """
    Write the chrom offset information (bin offset information written later)
    """
    grp.create_dataset(
        'chrom_offset',
        shape=(len(chr_offsets),),
        dtype=CHROMOFFSET_DTYPE,
        data=chr_offsets,
        **H5OPTS)


def initialize_pixels(grp, n_bins):
    """
    Initialize the pixels datasets with a given max_size (expression from
    cooler). These are resizable datasets that are chunked with size of
    CHUNK_SIZE and initialized with lenth of init_size.
    """
    # max_size = n_bins * (n_bins - 1) // 2 + n_bins
    max_size = None
    init_size = 0
    grp.create_dataset('bin1_id',
                       dtype=BIN_DTYPE,
                       shape=(init_size,),
                       maxshape=(max_size,),
                       **H5OPTS)
    grp.create_dataset('bin2_id',
                       dtype=BIN_DTYPE,
                       shape=(init_size,),
                       maxshape=(max_size,),
                       **H5OPTS)
    grp.create_dataset('count',
                       dtype=COUNT_DTYPE,
                       shape=(init_size,),
                       maxshape=(max_size,),
                       **H5OPTS)


def write_bin1_offset(grp, bin1_offsets):
    """
    Write the bin1_offset information
    """
    grp.create_dataset(
        'bin1_offset',
        shape=(len(bin1_offsets),),
        dtype=BIN1OFFSET_DTYPE,
        data=bin1_offsets,
        **H5OPTS)


def write_pixels_chunk(outfile, resolution, chunks, multi_res):
    """
    Converts the chunks dumped from the hic file into a 2D pixels array,
    with columns bin1 (int idx), bin2 (int idx), and count (int).
    An offsets array is also generated, which gives the number of bins preceding
    any given bin (with regard to bin1 position). Thus, bin1[0] will always have
    an offset of 0 and bin1[1] will have an offset equal to the number of times
    bin1=0.
    The pixel array is sorted by bin1 first, then bin2. The following could be
    a 2D pixel array (where the columns refer to bin1, bin2, and count
    respectively)
    0   0   3
    0   1   1
    0   2   0
    0   3   1
    1   2   1
    1   3   2
    2   3   1

    After pixels group has been written and bin1_offsets written within
    the indexes group, the temporary chunks dataset is deleted.
    """

    with h5py.File(outfile, "r+") as h5file:
        h5resolution = h5file['resolutions'][str(resolution)] if multi_res else h5file
        n_bins = h5resolution.attrs['nbins']
        chunks.sort(order=['bin1_id', 'bin2_id'])
        nnz = chunks.shape[0]
        # write ordered chunk to pixels
        grp = h5resolution['pixels']
        for set_name in ['bin1_id', 'bin2_id', 'count']:
            dataset = grp[set_name]
            prev_size = dataset.shape[0]
            dataset.resize(prev_size + nnz, axis=0)
            dataset[prev_size:] = chunks[set_name]
        h5file.close()


def finalize_resolution_cool(outfile, resolution, multi_res):
    """
    Finally, write bin1_offsets and nnz attr for one resolution in the cool file
    """
    with h5py.File(outfile, "r+") as h5file:
        h5resolution = h5file['resolutions'][str(resolution)] if multi_res else h5file
        nnz = h5resolution['pixels']['count'].shape[0]
        n_bins = h5resolution.attrs['nbins']
        # now we need bin offsets
        # adapated from _writer.py in cooler
        offsets = np.zeros(n_bins + 1, dtype=BIN1OFFSET_DTYPE)
        curr_val = 0
        for start, length, value in zip(*rlencode(h5resolution['pixels']['bin1_id'], 1000000)):
            offsets[curr_val:value + 1] = start
            curr_val = value + 1
        offsets[curr_val:] = nnz
        write_bin1_offset(h5resolution['indexes'], offsets)
        # update attributes with nnz
        info = {'nnz': nnz}
        h5resolution.attrs.update(info)
        h5file.close()


def rlencode(array, chunksize=None):
    """
    Run length encoding.
    Based on http://stackoverflow.com/a/32681075, which is based on the rle
    function from R.

    TAKEN FROM COOLER

    Parameters
    ----------
    x : 1D array_like
        Input array to encode
    dropna: bool, optional
        Drop all runs of NaNs.

    Returns
    -------
    start positions, run lengths, run values

    """
    where = np.flatnonzero
    if not isinstance(array, h5py.Dataset):
        array = np.asarray(array)
    n = len(array)
    if n == 0:
        return (np.array([], dtype=int),
                np.array([], dtype=int),
                np.array([], dtype=array.dtype))

    if chunksize is None:
        chunksize = n

    starts, values = [], []
    last_val = np.nan
    for i in range(0, n, chunksize):
        x = array[i:i+chunksize]
        locs = where(x[1:] != x[:-1]) + 1
        if x[0] != last_val:
            locs = np.r_[0, locs]
        starts.append(i + locs)
        values.append(x[locs])
        last_val = x[-1]
    starts = np.concatenate(starts)
    lengths = np.diff(np.r_[starts, n])
    values = np.concatenate(values)

    return starts, lengths, values


def get_unicode_utcnow():
    """
    datetime.utcnow().isoformat() will return bytestring with python 2
    """
    date_str = datetime.utcnow().isoformat()
    if not isinstance(date_str, type(u'')):
        return date_str.decode('utf-8')
    return date_str


def write_zooms_for_higlass(h5res):
    """
    NOT CURRENTLY USED

    Add max-zoom column needed for higlass, but only if the resolutions are
    successively divisible by two. Otherwise, warn that this file will not be
    higlass compatible
    """
    resolutions = sorted(f['resolutions'].keys(), key=int, reverse=True)

    # test if resolutions are higlass compatible
    higlass_compat = True
    for i in range(len(resolutions)):
        if i == len(resolutions) - 1:
            break
        if resolutions[i] * 2 != resolutions[i+1]:
            higlass_compat = False
            break
    if not higlass_compat:
        print_stderr('!!! WARNING: This hic file is not higlass compatible! Will not add [max-zoom] attribute.')
        return

    print('... INFO: This hic file is higlass compatible! Adding [max-zoom] attribute.')
    max_zoom = len(resolutions) - 1

    # Assign max-zoom attribute
    h5res.attrs['max-zoom'] = max_zoom
    print('... max-zoom: {}'.format(max_zoom))

    # Make links to zoom levels
    for i, res in enumerate(resolutions):
        print('... zoom {}: {}'.format(i, res))
        h5res[str(i)] = h5py.SoftLink('/resolutions/{}'.format(res))


def print_formatted_updates(updates, writefile):
    """
    Simple function to display updates for the user to confirm
    Updates are populated using prepare_hic2cool_updates
    """
    print('### Updates found. Will upgrade hic2cool file to version %s' % __version__)
    print('### This is what will change:')
    for upd in updates:
        print('- %s\n    - Effect: %s\n    - Detail: %s' % (upd['title'], upd['effect'], upd['detail']))
    print('### Will write to %s\n### Continue? [y/n]' % writefile)


def run_hic2cool_updates(updates, infile, writefile):
    """
    Actually run the updates given by the updates array
    """
    # create a copy of the infile, if writefile differs
    if infile != writefile:
        shutil.copy(infile, writefile)
    print('### Updating...')
    for upd in updates:
        print('... Running: %s' % upd['title'])
        upd['function'](writefile)
        print('... Finished: %s' % upd['title'])
    # now update the generated-by attr and add update-d
    generated_by = 'hic2cool-' + __version__
    update_data = get_unicode_utcnow()
    with h5py.File(writefile, 'r+') as h5_file:
        if 'resolutions' in h5_file:
            for res in h5_file['resolutions']:
                h5_file['resolutions'][res].attrs['generated-by'] = generated_by
                h5_file['resolutions'][res].attrs['update-date'] = update_data
                print('... Updated metadata for resolution %s' % res)
        else:
            h5_file.attrs['generated-by'] = generated_by
            h5_file.attrs['update-date'] = update_data
            print('... Updated metadata')
        h5_file.close()
    print('### Finished! Output written to: %s' % writefile)


def hic2cool_convert(infile, outfile, resolution=0, norm="", nproc=1, show_warnings=False, silent=False):
    """
    Main function that coordinates the reading of header and footer from infile
    and uses that information to parse the hic matrix.
    Opens outfile and writes in form of .cool file

    Params:
    <infile> str .hic filename
    <outfile> str .cool output filename
    <resolution> int bp bin size. If 0, use all. Defaults to 0.
                Final .cool structure will change depending on this param (see README)
    <show_warnings> bool. If True, print out WARNING messages
    <silent> bool. If true, hide standard output
    <nproc> number of processes to use
    """
    unit = 'BP'  # only using base pair unit for now
    resolution = int(resolution)
    # Global hic normalization types used
    global NORMS
    NORMS = []
    global WARN
    WARN = False
    req = open(infile, 'rb')
    reqarr = []
    for i in range(0, nproc):
        reqarr.append(open(infile, 'rb'))
    mmap_buf = mmap.mmap(req.fileno(), 0, access=mmap.ACCESS_READ)
    used_chrs, resolutions, footerPosition, genome, metadata = read_header(req)
    pair_footer_info, expected, factors, norm_info = read_footer(req, mmap_buf, footerPosition)
    # expected/factors unused for now
    del expected
    del factors
    # used to hold chr_chr key intersections missing from the hic file
    warn_chr_keys = []
    if not silent:  # print hic header info for command line usage
        chr_names = [used_chrs[key][1] for key in used_chrs.keys()]
        print('##########################')
        print('### hic2cool / convert ###')
        print('##########################')
        print('### Header info from hic')
        print('... Chromosomes: ', chr_names)
        print('... Resolutions: ', resolutions)
        print('... Normalizations: ', NORMS)
        print('... Genome: ', genome)
    # ensure user input binsize is a resolution supported by the hic file
    if resolution != 0 and resolution not in resolutions:
        error_str = (
            '!!! ERROR. Given binsize (in bp) is not a supported resolution in '
            'this file.\nPlease use 0 (all resolutions) or use one of: ' +
            str(resolutions))
        force_exit(error_str, req)
    use_resolutions = resolutions if resolution == 0 else [resolution]
    multi_res = len(use_resolutions) > 1
    # do some formatting on outfile filename
    # .mcool is the 4DN supported multi-res format, but allow .multi.cool too
    if outfile[-11:] == '.multi.cool':
        if not multi_res:
            outfile = ''.join([outfile[:-11] + '.cool'])
    elif outfile[-6:] == '.mcool':
        if not multi_res:
            outfile = ''.join([outfile[:-6] + '.cool'])
    elif outfile[-5:] == '.cool':
        if multi_res:
            outfile = ''.join([outfile[:-5] + '.mcool'])
    else:
        # unexpected file ending. just append .cool or .cool
        if multi_res:
            outfile = ''.join([outfile + '.mcool'])
        else:
            outfile = ''.join([outfile + '.cool'])
    # check if the desired path exists. try to remove, if so
    if os.path.exists(outfile):
        try:
            os.remove(outfile)
        except OSError:
            error_string = ("!!! ERROR. Output file path %s already exists. This"
                " can cause issues with the hdf5 structure. Please remove that"
                " file or choose a different output name." % (outfile))
            force_exit(error_string, req)
        if WARN:
            print_stderr('!!! WARNING: removed pre-existing file: %s' % (outfile))
    req.close()
    print('### Converting')
    pool = Pool(processes=nproc)
    for binsize in use_resolutions:
        t_start = time.time()
        # initialize cooler file. return per resolution bin offset maps
        chr_offset_map, chr_bins = initialize_res(outfile, infile, mmap_buf, unit, used_chrs,
                                                  genome, metadata, binsize, norm_info, norm, multi_res, show_warnings)
        covered_chr_pairs = []
        for chr_a in used_chrs:
            total_chunk = np.zeros(shape=0, dtype=CHUNK_DTYPE)
            if used_chrs[chr_a][1].lower() == 'all':
                continue
            for chr_b in used_chrs:
                if used_chrs[chr_b][1].lower() == 'all':
                    continue
                c1 = min(chr_a, chr_b)
                c2 = max(chr_a, chr_b)
                chr_key = str(c1) + "_" + str(c2)
                # since matrices are upper triangular, no need to cover c1-c2
                # and c2-c1 reciprocally
                if chr_key in covered_chr_pairs:
                    continue
                tmp_chunk = parse_hic(infile, pool, nproc, chr_key, unit, binsize,
                                      pair_footer_info, chr_offset_map, chr_bins,
                                      used_chrs, show_warnings, mmap_buf, reqarr)
                total_chunk = np.concatenate((total_chunk, tmp_chunk), axis=0)
                del tmp_chunk
                covered_chr_pairs.append(chr_key)
            # write at the end of every chr_a
            write_pixels_chunk(outfile, binsize, total_chunk, multi_res)
            del total_chunk
        # finalize to remove chunks and write a bit of metadata
        finalize_resolution_cool(outfile, binsize, multi_res)
        t_parse = time.time()
        elapsed_parse = t_parse - t_start
        if not silent:
            print('... Resolution %s took: %s seconds.' % (binsize, elapsed_parse))
    for i in range(0, nproc):
        reqarr[i].close()
    pool.close()
    pool.join()

    if not silent:
        if WARN and not show_warnings:
            print('... Warnings were found in this run. Run with -v to display them.')
        print('### Finished! Output written to: %s' % outfile)
        if multi_res:
            print('... This file is higlass compatible.')
        else:
            print('... This file is single resolution and NOT higlass compatible. Run with `-r 0` for multi-resolution.')


def hic2cool_extractnorms(infile, outfile, exclude_mt=False, show_warnings=False, silent=False):
    """
    Find all normalization vectors in the given hic file at all resolutions and
    attempts to add them to the given cooler file. Does not add any metadata
    to the cooler file. TODO: should we add `extract-norms-date` attr?

    Params:
    <infile> str .hic filename
    <outfile> str .cool output filename
    <exclude_mt> bool. If True, ignore MT contacts. Defaults to False.
    <show_warnings> bool. If True, print out WARNING messages
    <silent> bool. If true, hide standard output
    """
    unit = 'BP' # only using base pair unit for now
    # Global hic normalization types used
    global NORMS
    NORMS = []
    global WARN
    WARN = False
    req = open(infile, 'rb')
    buf = mmap.mmap(req.fileno(), 0, access=mmap.ACCESS_READ)
    used_chrs, resolutions, footerPosition, genome, metadata = read_header(req)
    pair_footer_info, expected, factors, norm_info = read_footer(req, buf, footerPosition)
    # expected/factors unused for now
    del expected
    del factors

    chr_names = [used_chrs[key][1] for key in used_chrs.keys()]
    if not silent: # print hic header info for command line usage
        print('################################')
        print('### hic2cool / extract-norms ###')
        print('################################')
        print('Header info from hic:')
        print('... Chromosomes: ', chr_names)
        print('... Resolutions: ', resolutions)
        print('... Normalizations: ', NORMS)
        print('... Genome: ', genome)

    if exclude_mt: # remove mitchondrial chr by name if this flag is set
        # try to find index of chrM (a.k.a chrMT) if it is present
        mt_names = ['m', 'mt', 'chrm', 'chrmt']
        found_idxs = [idx for idx, fv in used_chrs.items() if fv[1].lower() in mt_names]
        if len(found_idxs) == 1:
            excl = used_chrs.pop(found_idxs[0], None)
            if not silent: print('... Excluding chromosome %s with index %s' % (excl[1], excl[0]))
        if len(found_idxs) > 1:
            error_str = (
                'ERROR. More than one chromosome was found when attempting to'
                ' exclude MT. Found chromosomes: %s' % chr_names
            )
            force_exit(error_str, req)
        else:
            if not silent: print('... No chromosome found when attempting to exclude MT.')

    # exclude 'all' from chromsomes
    chromosomes = [uc[1] for uc in used_chrs.values() if uc[1].lower() != 'all']
    lengths = [uc[2] for uc in used_chrs.values() if uc[1].lower() != 'all']
    chromsizes = pd.Series(index=chromosomes, data=lengths)

    cooler_groups = {}
    for path in cooler.fileops.list_coolers(outfile):
        binsize = cooler.Cooler(outfile + '::' + path).info['bin-size']
        cooler_groups[binsize] = path
    if not silent:
        print('### Found cooler contents:')
        print('... %s' % cooler_groups)

    for norm in NORMS:
        for binsize in resolutions:
            if binsize not in cooler_groups:
                if not silent:
                    print('... Skip resolution %s; it is not in cooler file' % binsize)
                continue
            if not silent:
                print('... Extracting %s normalization vector at %s BP' % (norm, binsize))
            chrom_map = {}
            bins = cooler.binnify(chromsizes, binsize)
            lengths_in_bins = bins.groupby('chrom').size()
            for chr_val in [uc for uc in used_chrs.values() if uc[1].lower() != 'all']:
                chr_num_bins = lengths_in_bins.loc[chr_val[1]]
                try:
                    norm_key = norm_info[norm, unit, binsize, chr_val[0]]
                except KeyError:
                    WARN = True
                    if show_warnings and not silent:
                        print_stderr('!!! WARNING. Normalization vector %s does not exist for %s.'
                              % (norm, chr_val[1]))
                    # add a vector of 0's with length equal to by_chr_bins[chr_idx]
                    norm_vector = [np.nan] * chr_num_bins
                else:
                    norm_vector = read_normalization_vector(req, buf, norm_key)[:chr_num_bins]
                chrom_map[chr_val[1]] = norm_vector

            # hic normalization vector lengths have inconsistent lengths...
            # truncate appropriately
            bins[norm] = np.concatenate([chrom_map[chrom] for chrom in chromosomes])
            if not silent:
                print('... Writing to cool file ...')
                print('%s\n... Truncated ...' % bins.head())
            group_path = cooler_groups[binsize]
            cooler.create.append(
                outfile + '::' + group_path,
                'bins',
                {norm: bins[norm].values},
                force=True
            )
    req.close()
    if not silent:
        if WARN and not show_warnings:
            print('... Warnings were found in this run. Run with -v to display them.')
        print('### Finished! Output written to: %s' % outfile)


def hic2cool_update(infile, outfile='', show_warnings=False, silent=False):
    """
    Main function that reads the version of a given input cool file produced
    by hic2cool and performs different upgrading operations.
    Adds 'update-date' metadata to cooler attributes

    Params:
    <infile> str .cool input filename
    <outfile> str outpul filename (optional)
    <show_warnings> bool. If True, print out WARNING messages
    <silent> bool. If true, hide standard output
    """
    if not silent:
        print('#########################')
        print('### hic2cool / update ###')
        print('#########################')
    # open the file, find the resolutions and used hic2cool version
    with h5py.File(infile, 'r+') as h5_in:
        if 'resolutions' in h5_in:
            h5_attrs = {}
            resolutions = list(h5_in['resolutions'])
            # populate h5_attrs
            for res in resolutions:
                found_attrs = dict(h5_in['resolutions'][res].attrs)
                if not h5_attrs:
                    h5_attrs.update(found_attrs)
                else:
                    # ensure found version matches between resolutions
                    if found_attrs.get('generated_by') != h5_attrs.get('generated_by'):
                        error_string = "!!! ERROR. Input file has inconsistent 'generated-by' attributes"
                        force_exit(error_string)
        else:
            h5_attrs = dict(h5_in.attrs)
            resolutions = [int(h5_attrs['bin-size'])]
    if 'generated-by' not in h5_attrs:
        WARN = True
        error_string = "!!! ERROR. Input file doesn't seem to made by hic2cool. Exiting."
        force_exit(error_string)

    # parse out version info
    h2c_version = h5_attrs['generated-by']
    if not h2c_version.startswith('hic2cool-'):
        error_string = "!!! ERROR. Malformed 'generated-by' attribute: %s" % h2c_version
        force_exit(error_string)
    version_str = h2c_version[9:].split('.')
    if len(version_str) != 3 or any([ver for ver in version_str if not ver.isdigit()]):
        error_string = "!!! ERROR. Malformed 'generated-by' attribute: %s" % h2c_version
        force_exit(error_string)
    version_nums = [int(x) for x in version_str]
    creation_time = datetime.strptime(h5_attrs['creation-date'],
                                      '%Y-%m-%dT%H:%M:%S.%f').replace(microsecond=0)
    h2c_created = datetime.strftime(creation_time, '%Y-%m-%d at %H:%M:%S')
    if h5_attrs.get('update-date'):
        update_time = datetime.strptime(h5_attrs['update-date'],
                                        '%Y-%m-%dT%H:%M:%S.%f').replace(microsecond=0)
        h2c_updated = datetime.strftime(update_time, '%Y-%m-%d at %H:%M:%S')
    else:
        h2c_updated = 'None'
    if not silent:
        print('### Info from input file')
        print('... File path: %s' % infile)
        print('... Found creation timestamp: %s' % h2c_created)
        print('... Found update timestamp: %s' % h2c_updated)
        print('... Found resolutions: %s' % resolutions)
        print('... Found version: %s' % h2c_version[9:])
        print('... Target version: %s' % __version__)
    writefile = outfile if outfile else infile
    updates = prepare_hic2cool_updates(version_nums)
    if not updates:
        print('### No updates found!\n... Exiting')
        return
    # make sure user is happy with the updates
    if not silent:
        print_formatted_updates(updates, writefile)
        resp = ''
        while not resp or resp.lower() not in ['y', 'n', 'yes', 'no']:
            resp = use_input('[y/n] ')
        if resp.lower() in ['n', 'no']:
            return
    run_hic2cool_updates(updates, infile, writefile)


def memory_usage():
    """
    Testing utility
    print(str(memory_usage()))
    """
    import psutil
    import os
    prc = psutil.Process(os.getpid())
    # in MB
    mem = prc.memory_info()[0] / float(2 ** 20)
    return mem


def print_stderr(message):
    """
    Simply print str message to stderr
    """
    print(message, file=sys.stderr)


def force_exit(message, req=None):
    """
    Exit the program due to some error. Print out message and close the given
    input files.
    """
    if req:
        req.close()
    print_stderr(message)
    sys.exit(1)
