"""Feature-side processors written here rather than taken from Qlib.

Only one, and it exists because Qlib's same-named processor does something else.

`qlib.data.dataset.processor.ProcessInf` reads as "handle infinities", but its
implementation replaces each infinity with the cross-sectional mean of the
column — the source carries its own `FIXME: Such behavior is very weird`. That
is imputation, and ticket 07 deliberately does not impute: a suspended security
whose features cannot be computed must not be handed to the model dressed as an
average one. LightGBM learns a direction for missing values, which is a real
capability and a more honest answer than a fabricated number.

So `InfToNaN` converts `±inf` to NaN and does nothing else. Infinities still have
to go — unlike NaN they are not a missing-value signal to LightGBM, they are an
extreme value that will capture a split point and distort the tree.

The instance is passed to `DataHandlerLP` directly rather than as a
`{"class": ...}` config, so `init_instance_by_config` never resolves a name for
it and no module path leaves this file.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from qlib.data.dataset.processor import Processor, get_group_columns

#: Bumped whenever `InfToNaN.__call__` changes what it computes.
#:
#: A manual version, which this codebase otherwise avoids — feature sets and
#: parameter sets put their expanded definitions into the fingerprint instead.
#: The difference is that those are data and can be expanded; a processor is
#: code, and hashing its source would move the fingerprint on a reformat or a
#: comment. `test_model_definition.py` asserts that changing this number changes
#: the experiment fingerprint, which is the whole protection.
INF_TO_NAN_SEMANTICS_VERSION = 1


class InfToNaN(Processor):
    """Replace `±inf` with NaN, leaving every other value untouched.

    Stateless and row-local: it observes no other row, no other date and no
    other security, so it cannot carry information across a segment boundary.
    That is why the pipeline has nothing for the fit-window contract to bite on
    (see `ModelResearchDefinition.fit_window`).
    """

    def __init__(self, fields_group: str | None = None) -> None:
        self.fields_group = fields_group

    def __call__(self, df: pd.DataFrame) -> pd.DataFrame:
        columns = get_group_columns(df, self.fields_group)
        df[columns] = df[columns].replace([np.inf, -np.inf], np.nan)
        return df

    def is_for_infer(self) -> bool:
        return True

    def readonly(self) -> bool:
        # It rewrites values in place, so the handler must not hand it a frame
        # it intends to reuse.
        return False
