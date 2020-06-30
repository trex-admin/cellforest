import pytest
import pandas as pd

from cellforest import CellForest
from tests.fixtures import *


def test_normalize(root_path):
    spec = {
        "normalize": {
            "min_genes": 5,
            "max_genes": 5000,
            "min_cells": 5,
            "perc_mito_cutoff": 20,
            "method": "seurat_default",
        },
    }
    cf = CellForest(root_dir=root_path, spec_dict=spec)
    cf.process.normalize()
    pass