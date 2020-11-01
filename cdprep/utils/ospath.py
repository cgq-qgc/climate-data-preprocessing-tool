# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# Copyright © Climate Data Preprocessing Tool Project Contributors
# https://github.com/cgq-qgc/climate-data-preprocessing-tool
#
# This file is part of Climate Data Preprocessing Tool.
# Licensed under the terms of the GNU General Public License.
# -----------------------------------------------------------------------------

# ---- Standard imports
import os
import os.path as osp
from shutil import rmtree


def delete_file(filename):
    """Try to delete a file on the disk and return the error if any."""
    try:
        os.remove(filename)
        return None
    except OSError as e:
        print("Error: %s - %s." % (e.filename, e.strerror))
        return e.strerror


def delete_folder_recursively(dirpath, delroot=False):
    """Try to delete all files and sub-folders below the given dirpath."""
    if osp.exists(dirpath):
        for filename in os.listdir(dirpath):
            filepath = os.path.join(dirpath, filename)
            try:
                rmtree(filepath)
            except OSError:
                delete_file(filepath)
        if delroot:
            os.rmdir(dirpath)
