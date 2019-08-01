#!/usr/bin/env python3
# ***** BEGIN LICENSE BLOCK *****
# Version: MPL 2.0
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Contributors:
#  Christian Holler <choller@mozilla.com> (Original Developer)
#
# ***** END LICENSE BLOCK *****

import argparse
import json
import os
import requests
import sys

symbols = {}
filemap = {}

line_symbols_cache = {}

SOCORRO_AUTH_TOKEN = os.getenv("SOCORRO_AUTH_TOKEN")


def load_symbols(module, symfile):
    if module not in symbols:
        symbols[module] = []

    if symfile not in line_symbols_cache:
        line_symbols_cache[symfile] = []

    with open(symfile, 'r') as symfile_fd:
        for line in symfile_fd:
            line = line.rstrip()
            if line.startswith("MODULE "):
                pass
            elif line.startswith("FILE "):
                # FILE 14574 hg:hg.mozilla.org/try:xpcom/io/nsLocalFileCommon.cpp:8ff5f360a1909a75f636e93860aa456625df25f7
                tmp = line.split(" ", maxsplit=2)
                if symfile not in filemap:
                    filemap[symfile] = {}
                # FILE definitions are *not* per module as one would expect,
                # but actually per symbols file (so the same FILE id can appear
                # multiple times per module, in distinct symbols files).
                filemap[symfile][tmp[1]] = tmp[2]
            elif line.startswith("PUBLIC "):
                # Not supported currently
                pass
            elif line.startswith("STACK "):
                pass
            elif line.startswith("INFO "):
                pass
            elif line.startswith("FUNC "):
                # FUNC 8e5440 14e 0 webrtc::AudioProcessingImpl::Initialize
                tmp = line.split(" ", maxsplit=4)
                comps = [int(tmp[1], 16), int(tmp[2], 16), tmp[4], symfile]
                symbols[module].append(comps)
            else:
                # This is a line entry:
                # address size line filenum
                # a51fd3 35 433 14574
                line_symbols_cache[symfile].append(line)


def load_symbols_recursive(symbols_dir):
        for (path, dirs, files) in os.walk(symbols_dir):
            for file in files:
                fp_file = os.path.join(path, file)

                if fp_file.endswith(".sym"):
                    rel_file = fp_file.replace(symbols_dir, "", 1)
                    comps = rel_file.split(os.sep)
                    module = os.path.splitext(comps[-1])[0]

                    load_symbols(module, fp_file)


def retrieve_file_line_data_linear(symbol_entry, reladdr):
    # We reopen the symbols file and retrieve line data on the fly
    # because storing all of it in load_symbols is very memory intense.
    symfile = symbol_entry[3]
    with open(symfile, 'r') as symfile_fd:
        for line in symfile_fd:
            tmp = line.split(" ", maxsplit=3)
            try:
                start_addr = int(tmp[0], 16)
                if start_addr <= reladdr:
                    size = int(tmp[1], 16)
                    if (start_addr + size) > reladdr:

                        return (tmp[2], tmp[3].rstrip())
            except ValueError:
                # Ignore any non-line entries
                pass
    return (None, None)


def retrieve_file_line_data_binsearch(symbol_entry, reladdr):
    symfile = symbol_entry[3]
    lines = line_symbols_cache[symfile]

    if not lines:
        return (None, None)

    cmin = 0
    cmax = len(lines) - 1
    cidx = int(len(lines) / 2)

    while (cmax - cmin) >= 0 and cidx <= cmax:
        line = lines[cidx]
        tmp = line.split(" ", maxsplit=3)
        try:
            start_addr = int(tmp[0], 16)
            if start_addr <= reladdr:
                size = int(tmp[1], 16)
                if (start_addr + size) > reladdr:
                    return (tmp[2], tmp[3].rstrip())
                else:
                    cmin = cidx + 1
                    cidx = cmin + int((cmax - cmin) / 2)
            else:
                cmax = cidx - 1
                cidx = cmin + int((cmax - cmin) / 2)
        except ValueError:
            # Ignore any non-line entries
            cidx += 1

    return (None, None)


def retrieve_file_line_data(symbol_entry, reladdr):
    return retrieve_file_line_data_binsearch(symbol_entry, reladdr)


def read_extra_file(extra_file):
    def make_stack_array(line):
        return [int(x) for x in line.rstrip().split(sep="=")[1].split(",")]

    alloc_stack = None
    free_stack = None
    modules = None

    with open(extra_file, 'r') as extra_file_fd:
        for line in extra_file_fd:
            if line.startswith("PHCAllocStack"):
                alloc_stack = make_stack_array(line)
            elif line.startswith("PHCFreeStack"):
                free_stack = make_stack_array(line)
            elif line.startswith("StackTraces"):
                obj = json.loads(line.split(sep="=")[1])
                modules = obj["modules"]

    module_memory_map = {}

    for module in modules:
        module_memory_map[module["filename"]] = (int(module["base_addr"], 16), int(module["end_addr"], 16))

    return (alloc_stack, free_stack, module_memory_map)


def fetch_socorro_crash(crash_id):
    headers = {'Auth-Token': SOCORRO_AUTH_TOKEN}

    raw_url = 'https://crash-stats.mozilla.org/api/RawCrash/?crash_id=%s&format=meta' % crash_id
    processed_url = 'https://crash-stats.mozilla.org/api/ProcessedCrash/?crash_id=%s&datatype=processed' % crash_id

    response = requests.get(raw_url, headers=headers)

    if not response.ok:
        print("Error: Failed to fetch raw data from Socorro", file=sys.stderr)
        return (None, None, None)

    raw_data = response.json()

    if "PHCAllocStack" not in raw_data:
        print("Error: No PHCAllocStack in raw data, is this really a PHC crash?", file=sys.stderr)
        return (None, None, None)

    alloc_stack = [int(x) for x in raw_data["PHCAllocStack"].split(",")]
    free_stack = [int(x) for x in raw_data["PHCFreeStack"].split(",")]

    response = requests.get(processed_url, headers=headers)

    if not response.ok:
        print("Error: Failed to fetch processed data from Socorro", file=sys.stderr)
        return (None, None, None)

    processed_data = response.json()

    module_memory_map = {}
    remote_symbols_files = set()

    for module in processed_data["json_dump"]["modules"]:
        module_memory_map[module["filename"]] = (int(module["base_addr"], 16), int(module["end_addr"], 16))
        if "symbol_url" in module:
            remote_symbols_files.add(module["symbol_url"])

    return (alloc_stack, free_stack, module_memory_map, remote_symbols_files)


def fetch_remote_symbols(url, symbols_dir):
    url_comps = url.split("/")
    dest_dir = os.path.join(symbols_dir, url_comps[-2])

    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir)

    dest_file = os.path.join(dest_dir, url_comps[-1])

    sys.stderr.write("Fetching %s ... " % url)

    if os.path.exists(dest_file):
        print(" cached!", file=sys.stderr)
        return

    response = requests.get(url)
    print("done!", file=sys.stderr)

    with open(dest_file, 'w') as fd:
        fd.write(response.text)

    return


def main(argv=None):
    '''Command line options.'''

    program_name = os.path.basename(sys.argv[0])

    if argv is None:
        argv = sys.argv[1:]

    # setup argparser
    parser = argparse.ArgumentParser(usage='%s (EXTRA_FILE SYMBOLS_DIR | --remote CRASH_ID)' % program_name)
    parser.add_argument("--remote", dest="remote", help="Remote mode, fetch a crash from Socorro", metavar="CRASH_ID")
    parser.add_argument('rargs', nargs=argparse.REMAINDER)

    if not argv:
        parser.print_help()
        return 2

    opts = parser.parse_args(argv)

    if not opts.remote and len(opts.rargs) < 2:
        parser.print_help()
        return 2

    # We need two stacks, the allocation stack and the free stack
    alloc_stack = None
    free_stack = None

    # The module memory map contains all the information about loaded
    # modules and their address ranges, required to resolve absolute
    # addresses to relative debug symbol addresses.
    module_memory_map = None

    # Directory where we either have local symbols or store remote symbols
    symbols_dir = None

    if opts.remote:
        if SOCORRO_AUTH_TOKEN is None:
            print("Error: Must specify SOCORRO_AUTH_TOKEN in environment for remote actions.", file=sys.stderr)
            return 2

        (alloc_stack, free_stack, module_memory_map, remote_symbols_files) = fetch_socorro_crash(opts.remote)

        symbols_dir = os.path.join(os.path.expanduser("~"), ".phc-symbols-cache")
        symbols_dir += os.sep

        if not os.path.exists(symbols_dir):
            os.mkdir(symbols_dir)

        for symbol_url in remote_symbols_files:
            fetch_remote_symbols(symbol_url, symbols_dir)

        sys.stderr.write("Loading downloaded symbols...")
        load_symbols_recursive(symbols_dir)
        print(" done!", file=sys.stderr)
    else:
        extra_file = opts.rargs[0]
        symbols_dir = opts.rargs[1]

        if not os.path.isfile(extra_file):
            print("Invalid .extra file specified", file=sys.stderr)
            return 2

        if not os.path.isdir(symbols_dir):
            print("Invalid symbols directory specified", file=sys.stderr)
            return 2

        if not symbols_dir.endswith(os.sep):
            symbols_dir += os.sep

        sys.stderr.write("Loading local symbols...")
        load_symbols_recursive(symbols_dir)
        print(" done!", file=sys.stderr)

        (alloc_stack, free_stack, module_memory_map) = read_extra_file(extra_file)

    def print_stack(phc_stack, name, symbols, module_memory_map):
        stack_cnt = 0

        print("%s stack:" % name)
        print("")
        for addr in phc_stack:
            # Step 1: Figure out which module this address belongs to
            (module, reladdr) = (None, None)
            for module_cand in module_memory_map:
                base_addr = module_memory_map[module_cand][0]
                end_addr = module_memory_map[module_cand][1]

                if addr >= base_addr and addr < end_addr:
                    module = module_cand
                    reladdr = addr - base_addr
                    break

            if not module:
                print("#%s    (frame in unknown module)" % stack_cnt)
                stack_cnt += 1
                continue

            if module not in symbols:
                print("#%s    (missing symbols for module %s)" % (stack_cnt, module))
                stack_cnt += 1
                continue

            symbol_entry = None
            for sym in symbols[module]:
                if sym[0] <= reladdr and (sym[0] + sym[1]) > reladdr:
                    print("#%s    %s" % (stack_cnt, sym[2]))
                    symbol_entry = sym
                    break

            if not symbol_entry:
                print("#%s    ??? (unresolved symbol in %s)" % (stack_cnt, module))
            else:
                (line, filenum) = retrieve_file_line_data_binsearch(symbol_entry, reladdr)
                symfile = symbol_entry[3]
                if filenum and symfile in filemap:
                    print("    in file %s line %s" % (filemap[symfile][filenum], line))

            stack_cnt += 1

    print("")
    print_stack(free_stack, "Free", symbols, module_memory_map)
    print("")
    print_stack(alloc_stack, "Alloc", symbols, module_memory_map)


if __name__ == '__main__':
    main()