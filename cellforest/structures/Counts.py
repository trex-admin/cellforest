import os
import pickle
from functools import wraps
from pathlib import Path
from typing import Union, Iterable, Optional, Callable, Tuple

from matplotlib.axes import Axes
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.axes._subplots import Axes
from scipy.sparse import csr_matrix, hstack, vstack

from cellforest.structures import const
from cellforest.structures.build_counts_store import build_counts_store
from cellforest.structures.exceptions import CellsNotFound, GenesNotFound
from cellforest.utils.cellranger import CellRangerIO
from cellforest.utils.r.Convert import Convert


class Counts(csr_matrix):
    _SUPPORTED_CHEMISTRIES = ["v1", "v2", "v3"]
    # TODO: change to singular
    FEATURES_COLUMNS = ["ensgs", "genes"]
    SUPER_METHODS = const.SUPER_METHODS
    _SUPPORTED_AGG_FUNCS = {
        "built-in": ["sum", "mean", "min", "max"],
        "derived": ["std", "var"],
        "all": ["sum", "mean", "min", "max", "std", "var"],
    }
    _SUPPORTED_AGG_AXES = ["cells", "genes", 0, 1, "0", "1"]

    def __init__(self, matrix, cell_ids, features, **kwargs):
        # TODO: make a get_counts function that just takes the directory
        super().__init__(matrix, **kwargs)
        self._matrix = matrix
        self.chemistry = "v3" if "mode" in features.columns else "v2"
        self.features = features.iloc[:, :2].copy()
        self.features.columns = self.FEATURES_COLUMNS
        self._idx = self._convert_to_series(cell_ids)
        self._ids = self._index_col_swap(cell_ids)

    @property
    def genes(self):
        return self.features["genes"]

    @property
    def ensgs(self):
        return self.features["ensgs"]

    @property
    def index(self):
        return self._idx

    @property
    def columns(self):
        return self.genes

    @property
    def cell_ids(self):
        return self.index

    @classmethod
    def concatenate(cls, counts_list: Union["Counts", Iterable["Counts"]], axis: int = 0) -> "Counts":
        counts_list = counts_list.copy()
        orig = counts_list.pop(0)
        return orig.append(counts_list, axis=axis)

    def append(self, others: Union["Counts", Iterable["Counts"]], axis: int = 0) -> "Counts":
        if axis == 0:
            return self.vstack(others)
        elif axis == 1:
            return self.hstack(others)

    def vstack(self, others: Union["Counts", Iterable["Counts"]]):
        others = others if isinstance(others, (list, tuple)) else [others]
        matrix = vstack([self._matrix, *[x._matrix for x in others]])
        cell_ids = pd.concat([self.cell_ids, *[x.cell_ids for x in others]]).reset_index(drop=True)
        features = self.features
        return self.__class__(matrix, cell_ids, features)

    def hstack(self, others: Union["Counts", Iterable["Counts"]]):
        others = others if isinstance(others, (list, tuple)) else [others]
        matrix = hstack([self._matrix, *[x._matrix for x in others]])
        cell_ids = self.cell_ids
        features = pd.concat([self.features, *[x.features for x in others]]).reset_index(drop=True)
        return self.__class__(matrix, cell_ids, features)

    def hist(
        self,
        agg: str = "sum",
        axis: int = 0,
        labels: Optional[Union[pd.Series, list]] = None,
        ax: Axes = None,
        **kwargs,
    ) -> Axes:
        """
        Plots histogram along specified axis, optionally, stratified by label
        Args:
            agg: name of aggregation function for opposite axis (e.g., "std"); all options: `self._SUPPORTED_AGG_FUNCS`
            axis: axis along which to create histogram, with `agg` applied to other axis
            labels: list or pd.Series of cell or gene category labels by which to stratify plot
            ax: matplotlib pyplot or axes object which defines the plot
            kwargs: keyword arguments for plt.hist()

        Returns:
            ax: histogram

        Examples:
            >>> rna = Counts.from_cellranger("../tests/data/v3_gz/sample_1")  # load Counts matrix
            >>> half_of_cells = rna._matrix.shape[0] // 2
            >>> labels = ["sample_1"] * half_of_cells + ["sample_2"] * half_of_cells  # create cell labels
            >>> fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 6))  # figure with 1x2 axes
            >>> rna.hist("sum", axis="cells", ax=ax1, labels=labels, bins=30, alpha=0.5, histtype='step')  # plot on 1st axes object
            >>> rna.hist("std", axis=0, ax=ax2)  # plot on 2nd axes object
            >>> fig.show()  # display figure state

            >>> rna = Counts.from_cellranger("../tests/data/v3_gz/sample_1")  # load Counts matrix
            >>> rna.hist("sum", axis=1, color="#7eaa53", bins=30)  # plot on currect (or newly created) axes object
            >>> plt.show()  # display current figure with axes
        """

        axis = self._get_numeric_axis(axis)
        labels = self._get_agg_labels(labels, axis)

        cells_axis = axis == 0  # bool for aggregated axis' name
        cs_matrix = (
            self._matrix.tocsr() if cells_axis else self._matrix.tocsc()
        )  # convert to compressed sparse column/row for fast arithmetics

        ax = ax or plt.gca()  # use defined or get current axes
        for label in set(labels):
            where_label = np.where(np.array(labels) == label)[0]
            matrix_slice = cs_matrix[where_label, :] if cells_axis else cs_matrix[:, where_label]
            rna_agg = self._agg_apply(matrix_slice, agg=agg, axis=axis)
            ax.hist(rna_agg, label=label, **kwargs)

        x_label = "transcript count" if cells_axis else "cell count"
        title = x_label + " " + ("per cell" if cells_axis else "per gene")
        ax.set_title("{} of ".format(agg) + title)
        ax.set_ylabel("quantity")
        ax.set_xlabel(x_label)
        ax.legend()

        return ax

    def scatter(
        self,
        agg_x: str = "sum",
        agg_y: str = "var",
        axis: int = 0,
        labels: Optional[Union[pd.Series, list]] = None,
        ax: Axes = None,
        **kwargs,
    ) -> plt.axes:
        """
        Plots scatterplot along specified axes, optionally, stratified by label
        Args:
            agg_x: aggregation function for x-axis (e.g. sum, min, mean, var, etc.); all options: `self._SUPPORTED_AGG_FUNCS`
            agg_y: aggregation function for y-axis (e.g. sum, min, mean, var, etc.); all options: `self._SUPPORTED_AGG_FUNCS`
            axis: axis along which to create scatterplot, with `agg` applied to other axis
            labels: list or pd.Series of cell or gene category labels by which to stratify plot
            ax: pyplot subplot or axes object which defines the plot
            kwargs: keyword arguments for plt.scatter()

        Returns:
            ax: 2D scatterplot

        Examples:
            >>> rna = Counts.from_cellranger("../tests/data/v3_gz/sample_1")  # load Counts matrix
            >>> fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 6))  # figure with 1x2 axes
            >>> rna.scatter(agg_x="mean", agg_y="var", axis="cells", ax=ax1)  # plot on 1st axes object
            >>> rna.scatter("mean", "var", axis="genes", ax=ax2)  # plot on 2nd axes object
            >>> fig.show()  # display figure state

            >>> num_genes = rna._matrix.shape[1]
            >>> labels = ["sample_1"] * (num_genes // 2) + ["sample_2"] * (num_genes // 2)
            >>> rna.scatter("sum", "std", axis="genes", labels=labels, alpha=0.2)
            >>> plt.show()
        """

        axis = self._get_numeric_axis(axis)
        labels = self._get_agg_labels(labels, axis)

        cells_axis = axis == 0  # bool for aggregated axis' name
        cs_matrix = (
            self._matrix.tocsr() if cells_axis else self._matrix.tocsc()
        )  # convert to compressed sparse column/row for fast arithmetics

        ax = ax or plt.gca()  # use defined or get current axes
        for label in set(labels):
            where_label = np.where(np.array(labels) == label)[0]
            matrix_slice = cs_matrix[where_label, :] if cells_axis else cs_matrix[:, where_label]
            rna_agg_x = self._agg_apply(matrix_slice, agg=agg_x, axis=axis)
            rna_agg_y = self._agg_apply(matrix_slice, agg=agg_y, axis=axis)
            ax.scatter(rna_agg_x, rna_agg_y, label=label, **kwargs)

        ax_label = "transcript count" if cells_axis else "cell count"
        title = ax_label + " " + ("per cell" if cells_axis else "per gene")
        ax.set_title(agg_y + " vs " + agg_x + " of " + title)
        ax.set_xlabel(agg_x + " of " + ax_label)
        ax.set_ylabel(agg_y + " of " + ax_label)
        ax.legend()

        return ax

    def _get_numeric_axis(self, axis):
        """Get binary value for axis or check if it's out of range"""
        if axis in self._SUPPORTED_AGG_AXES:
            axis = self._SUPPORTED_AGG_AXES.index(axis) % 2  # convert to 0 and 1
        else:
            raise ValueError("axis must be in {}".format(self._SUPPORTED_AGG_AXES))

        return axis

    def _get_agg_labels(self, labels, axis):
        """Check if labels for aggregation are of correct length or return singular label (one sample)"""
        matrix_agg_len = self._matrix.get_shape()[axis]
        if labels == None:
            labels = ["sample"] * matrix_agg_len
        elif len(labels) != matrix_agg_len:  # check if labels length is the same as matrix axis length
            raise ValueError(
                "labels list of length {} cannot be broadcasted with matrix aggregation axis length {}".format(
                    len(labels), matrix_agg_len
                )
            )

        return labels

    def _agg_apply(self, matrix: np.matrix, agg: str, axis: int):
        """Apply aggregate function onto matrix along specified axis"""

        agg_axis = abs(1 - axis)  # to aggregate opposite axis

        if agg in self._SUPPORTED_AGG_FUNCS["built-in"]:
            agg_func = getattr(matrix, agg)
            rna_agg_out = agg_func(axis=agg_axis)
        elif agg in self._SUPPORTED_AGG_FUNCS["derived"]:
            # TODO: might run out of memory because there is conversion to numpy matrix in agg funcs
            rna_var = matrix.power(2).mean(axis=agg_axis) - np.power(matrix.mean(axis=agg_axis), 2)
            rna_agg_out = np.sqrt(rna_var) if agg == "std" else rna_var  # std is sqrt(var)
        else:
            raise NotImplementedError(
                'aggregation function "{0}" not supported, valid options are: {1}'.format(
                    agg, self._SUPPORTED_AGG_FUNCS["all"]
                )
            )

        rna_agg = np.ravel(rna_agg_out.sum(axis=agg_axis))  # flatten matrix
        return rna_agg

    @staticmethod
    def _get_agg_label(agg):
        mapping = {
            "sum": "",
            "mean": "mean",
            "std": "standard deviation of",
            "var": "variance of",
            "min": "minimum of",
            "max": "maximum of",
        }
        agg_name = mapping[agg] + "of"

        return agg_name

    def drop(self, indices, axis=0):
        """
        
        Args:
            indices:
            axis:

        Returns:
            counts_kept:
        """
        # TODO: QUEUE
        raise NotImplementedError()

    def dropna(self, axis=None):
        if axis is None:
            return self.dropna(axis=0).dropna(axis=1)
        sum_axis = int(not bool(axis))
        selector = np.asarray(self.sum(axis=sum_axis)).flatten().astype(bool)
        if axis == 0:
            return self[selector]
        else:
            return self[:, selector]

    def to_df(self):
        return pd.DataFrame(self.todense(), columns=self.columns, index=self.index)

    @classmethod
    def from_cellranger(cls, cellranger_dir):
        """Load from 10X Cellranger output format"""
        crio = CellRangerIO(cellranger_dir)
        matrix = crio.read_matrix()
        cell_ids = crio.read_barcodes()
        features = crio.read_features()
        return cls(matrix, cell_ids, features)

    def to_cellranger(self, output_dir, gz=True, chemistry="v3"):
        """Save in 10X Cellranger output format"""
        output_dir = Path(output_dir)
        crio = CellRangerIO
        # TODO: memory duplication
        counts = self.as_chemistry_version(chemistry)
        features_filename = "features.tsv" if chemistry == "v3" else "genes.tsv"
        crio.write_matrix(output_dir / "matrix.mtx", counts._matrix, gz)
        crio.write_features(output_dir / features_filename, counts.features, gz)
        crio.write_barcodes(output_dir / "barcodes.tsv", counts.cell_ids, gz)

    @classmethod
    def from_rds(cls, path):
        """Convert rds to pickle, then load pickle"""
        # TODO: QUEUE - test (Convert.rds_to_pickle_dir may overwrite any existing metadata)
        raise NotImplementedError()
        parent = Path(path).parent
        Convert.rds_to_pickle_dir(parent)
        return cls.load(parent / "rna.pickle")

    def to_rds(self, path):
        """Save pickle, then convert pickle to rds"""
        # TODO: QUEUE - test (Convert.pickle_to_rds_dir may overwrite any existing metadata)
        raise NotImplementedError()
        path = Path(path)
        stem = path.stem
        self.save(path.parent / f"{stem}.pickle")
        Convert.pickle_to_rds_dir(path.parent)

    @classmethod
    def load(cls, filepath):
        """Load from pickle"""
        with open(filepath, "rb") as f:
            store = pickle.load(f)
        return cls(store.matrix, store.cell_ids, store.features)

    def save(self, filepath, create_rds=False):
        """
        Save as pickle.
        Intermediate data store object used to maintain future compatibility
        """
        self._save(filepath, self._matrix, self.cell_ids, self.features, create_rds)

    def copy(self):
        return self.__class__(self._matrix.copy(), self.cell_ids.copy(), self.features.copy())

    def as_chemistry_version(self, chemistry):
        """Duplicate with a different 10X chemistry version"""
        if chemistry not in self._SUPPORTED_CHEMISTRIES:
            raise ValueError(f"supported chemistries: {self._SUPPORTED_CHEMISTRIES}")
        counts = self.copy()
        counts.chemistry = chemistry
        if chemistry == "v3":
            counts.features["mode"] = "Gene Expression"
        else:
            if "mode" in counts.features.columns:
                counts.features.drop("mode", inplace=True)
        return counts

    def __getitem__(self, key: Union[pd.Series, list, str, int, tuple]):
        if isinstance(key, tuple):
            return self._2d_slice(key)
        else:
            return self._cell_slice(key)

    def _2d_slice(self, key):
        """Slice rows and columns (cells and genes)"""
        gene_sliced = self._gene_slice(key[1])
        cell_sliced = gene_sliced[key[0]]
        return cell_sliced

    def _cell_slice(self, key):
        """Slice rows (cells)"""
        try:
            key = self._convert_key(key, self._ids)
        except KeyError:
            raise CellsNotFound(self._ids, key)
        if isinstance(key, slice):
            cell_ids = pd.DataFrame(self._idx[key]).reset_index(drop=True)
        else:
            cell_ids = pd.DataFrame(self._idx.reindex(key)).reset_index(drop=True)
        mat = csr_matrix(self._matrix)[key]
        return self.__class__(mat, cell_ids, self.features)

    def _gene_slice(self, key):
        """Slice columns (genes) with either gene names or ensemble names"""
        key = self._genes_convert_key(key)
        mat = csr_matrix(self._matrix)[:, key]
        if isinstance(key, slice):
            features = self.features[key]
        else:
            genes = pd.DataFrame(self.genes.reindex(key)).reset_index(drop=True)
            ensgs = pd.DataFrame(self.ensgs.reindex(key)).reset_index(drop=True)
            if len(ensgs) > len(genes):
                features = self.features[self.features.ensgs.isin(key)]
            else:
                features = self.features[self.features.genes.isin(key)]
        return self.__class__(mat, self._idx, features)

    @property
    def _genes_names(self):
        return self._index_col_swap(self.genes.copy(), "genes")

    @property
    def _ensgs_names(self):
        return self._index_col_swap(self.ensgs.copy(), "ensgs")

    def _genes_convert_key(self, key):
        """"""
        try:
            key = self._convert_key(key, self._genes_names)
        except KeyError:
            try:
                key = self._convert_key(key, self._ensgs_names)
            except KeyError:
                genes_err = GenesNotFound(self._genes_names, key)
                ensgs_err = GenesNotFound(self._ensgs_names, key)
                if len(ensgs_err.missing) < len(genes_err.missing):
                    raise ensgs_err
                else:
                    raise genes_err
        return key

    @staticmethod
    def _convert_key(key, df):
        """Slice index dataframe with key and convert to integer indices"""
        if isinstance(key, (pd.Series, pd.Index, np.ndarray)):
            key = key.tolist()
        if isinstance(key, list):
            if isinstance(key[0], str):
                # gene names are duplicated, ensgs aren't
                if df.index.duplicated().any():
                    df_temp = df.reset_index()
                    key_rows = df_temp[df_temp[df_temp.columns[0]].isin(key)]
                else:
                    key_rows = df.reindex(key)
                key = key_rows.dropna()["i"].astype(int).tolist()
        elif isinstance(key, str):
            key = [df.loc[key]["i"].tolist()]
        elif isinstance(key, int):
            key = [key]
        else:
            return key
        if len(key) == 0:
            raise KeyError("No matching indices")
        return key

    @staticmethod
    def _check_key(key, df):
        """
        NOTE: not currently used because metedata includes cells that were filtered out by cellranger
        """
        intersection = len(set(key).intersection(set(df.index.tolist()))) / len(key)
        if intersection < 1:
            import ipdb

            ipdb.set_trace()
            raise KeyError(f"some of provided keys missing from counts matrix. Intersection: {intersection}")

    @staticmethod
    def _index_col_swap(df, col: Union[str, int] = 0, new_index_colname="i"):
        """Swaps column with index of DataFrame"""
        df = df.copy()
        if isinstance(df, pd.Series):
            df = pd.DataFrame(df)
        if isinstance(col, int):
            col = df.columns[col]
        df[new_index_colname] = df.index
        df.index = df[col]
        df.index.name = "cell_id"
        df.drop(columns=col, inplace=True)
        return df

    @staticmethod
    def _save(filepath, matrix, cell_ids, features, create_rds=False):
        filepath = Path(filepath)
        build_counts_store(matrix, cell_ids, features, save_path=filepath)
        if create_rds:
            Convert.pickle_to_rds_dir(filepath.parent)

    @staticmethod
    def _convert_to_series(df):
        """If a dataframe, convert to series"""
        if isinstance(df, pd.DataFrame):
            df = df.iloc[:, 0].copy()
        elif not isinstance(df, pd.Series):
            raise TypeError(f"Must be dataframe not series {type(df)}")
        return df

    def __repr__(self):
        return f"{self.__class__}: [cell_ids x genes] matrix\n" + csr_matrix.__repr__(self)

    def __len__(self):
        return self.shape[0]

    @staticmethod
    def wrap_super(func):
        """Wrapper to pass scipy matrix methods through to .matrix attribute"""

        @wraps(func)
        def wrapper(counts, *args, **kwargs):
            matrix = func(counts._matrix, *args, **kwargs)
            return counts.__class__(matrix, counts.cell_ids, counts.genes)

        return wrapper

    @staticmethod
    def decorate(method_names):
        """
        Wrap a list of scipy matrix `method_names` with `wrap_super` and
        re-tether them to class
        """
        for name in method_names:
            super_method = getattr(csr_matrix, name)
            wrapped_method = Counts.wrap_super(super_method)
            setattr(Counts, name, wrapped_method)


Counts.decorate(Counts.SUPER_METHODS)
