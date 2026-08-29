"""The feature sets a model experiment may be built from, registered in code.

Two constraints shape this module, and they pull in opposite directions.

*Nothing user-supplied may reach Qlib.* `init_instance_by_config` resolves a
class by importing whatever module path it is handed, so an expression or class
name that travelled through a request body or a database column would be
arbitrary code loading. Callers therefore name a feature set; they never
describe one.

*The name is not the identity.* A registry that fingerprints only
`("alpha158_jp_v1", 1)` cannot tell that someone edited an expression and forgot
to bump the version — two runs with different meaning would reuse one
`ResearchExperiment`. So the fingerprint takes the **expanded, ordered
definition**: every column's expression, the fields it reads, its window, its
dtype, and its position. Edit an expression and the experiment identity moves on
its own; add an unrelated feature set and it does not.

The expressions are Qlib's own Alpha158 / Alpha360, obtained from
`qlib.contrib.data.loader` rather than transcribed. Copying 158 strings into
this file would fork them silently on the next pyqlib bump; reading them keeps
the definitions honest, and the expansion that enters the fingerprint records
exactly what this pyqlib produced.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

# Every `$field` a bundle can offer (see `research/bundle.py`). An expression
# naming anything outside this set is a bug in the registry, not a user error,
# and is caught at registration time rather than deep inside a worker run.
KNOWN_BUNDLE_FIELDS = frozenset(
    {
        "open", "high", "low", "close", "volume", "factor", "vwap",
        "rawopen", "rawhigh", "rawlow", "rawclose", "rawvolume",
        "trading_value", "adjustment_event", "quality_status",
    }
)

#: `$close`, `$vwap`, ... — the fields an expression reads.
_FIELD_RE = re.compile(r"\$([a-z_][a-z0-9_]*)", re.IGNORECASE)

#: Integer arguments of any operator call: `Mean($close, 30)`, and also the
#: middle argument of `Quantile($close, 20, 0.8)`, which a "digit immediately
#: before the closing paren" pattern misses entirely.
#:
#: The lookahead rather than a literal `)` is the point. Floats are skipped for
#: free — `0.8)` cannot match `\d+` followed by `[,)]` — and so is the `1e-12`
#: guard term in the kbar expressions, which no comma precedes.
_WINDOW_ARG_RE = re.compile(r",\s*(\d+)\s*(?=[,)])")

#: `window` is counted in **lags**, matching `StockPoolPolicy.required_bar_offsets`
#: where ticket 06 states the 6-1 momentum endpoints as `(147, 21)`. Counting
#: bars-including-today instead would read one higher everywhere and push
#: `required_history_days` to 148, which changes `policy_fingerprint` and breaks
#: the property section 4.3 depends on: that 06 and 07 resolve the *same*
#: universe on the same day, which is what makes the momentum comparison valid.
#: So no `+1` for `Ref`/`Delta` — the literal is already the lag.

#: All feature values reach Qlib as float32 (`write_native_bundle`), so the
#: dtype is uniform. It is still recorded per column: it belongs to the identity
#: of the definition, and a future set with a categorical column must not be
#: able to slip in under an unchanged fingerprint.
FEATURE_DTYPE = "float32"


@dataclass(frozen=True)
class FeatureSpec:
    """One column: what it computes, what it reads, how far back it looks."""

    name: str
    expression: str
    required_fields: tuple[str, ...]
    window: int
    dtype: str = FEATURE_DTYPE

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "expression": self.expression,
            "required_fields": list(self.required_fields),
            "window": self.window,
            "dtype": self.dtype,
        }


@dataclass(frozen=True)
class FeatureSet:
    """An ordered list of columns, identified by its contents.

    `version` and `description` are for people reading a page. Neither takes
    part in identity — `definition_payload` does.
    """

    name: str
    version: str
    description: str
    features: tuple[FeatureSpec, ...]
    #: Product metadata driving what the API and page offer. Deliberately absent
    #: from `definition_payload`: they steer selection and wording, not what any
    #: column computes, so changing one must not mint a new experiment.
    selectable: bool = True
    is_default: bool = False
    maturity: str = "baseline"

    @property
    def max_window(self) -> int:
        return max((item.window for item in self.features), default=0)

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(sorted({field for item in self.features for field in item.required_fields}))

    @property
    def expressions(self) -> tuple[str, ...]:
        return tuple(item.expression for item in self.features)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.features)

    @property
    def definition_payload(self) -> dict[str, Any]:
        """What enters the experiment fingerprint and the artifact manifest.

        Column order is carried by the list itself; a reordering is a different
        definition because it is a different feature matrix.
        """
        return {
            "name": self.name,
            "version": self.version,
            "dtype": self.dtype_summary,
            "columns": [item.as_payload() for item in self.features],
        }

    @property
    def dtype_summary(self) -> str:
        dtypes = {item.dtype for item in self.features}
        return dtypes.pop() if len(dtypes) == 1 else "mixed"

    @property
    def definition_checksum(self) -> str:
        encoded = json.dumps(
            self.definition_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class UnknownFeatureSetError(LookupError):
    """The requested feature set is not registered.

    Its own type because the API turns it into a 400: naming a set that does not
    exist is a caller mistake, not a server fault.
    """


def _spec(name: str, expression: str) -> FeatureSpec:
    fields = tuple(sorted({match.lower() for match in _FIELD_RE.findall(expression)}))
    unknown = set(fields) - KNOWN_BUNDLE_FIELDS
    if unknown:
        raise ValueError(f"{name}: expression reads unknown bundle field(s) {sorted(unknown)}")
    windows = [int(value) for value in _WINDOW_ARG_RE.findall(expression)]
    return FeatureSpec(
        name=name,
        expression=expression,
        required_fields=fields,
        window=max(windows, default=0),
    )


def _from_qlib_loader(names: list[str], expressions: list[str]) -> tuple[FeatureSpec, ...]:
    return tuple(_spec(name, expression) for name, expression in zip(names, expressions))


@lru_cache(maxsize=1)
def _registry() -> dict[str, FeatureSet]:
    # Imported here, not at module scope: this module is read by the API process
    # to answer `GET /research/feature-sets`, and importing qlib there would
    # break the process seam that keeps Qlib inside the worker. The loader
    # module holds only expression strings and pulls in no provider state.
    from qlib.contrib.data.loader import Alpha158DL, Alpha360DL

    alpha158_expressions, alpha158_names = Alpha158DL.get_feature_config()
    alpha360_expressions, alpha360_names = Alpha360DL.get_feature_config()

    sets = (
        FeatureSet(
            name="alpha158_jp_v1",
            version="1",
            description="Qlib Alpha158 (kbar + price + rolling) over Japanese adjusted bars.",
            features=_from_qlib_loader(alpha158_names, alpha158_expressions),
            is_default=True,
        ),
        FeatureSet(
            name="alpha360_jp_v1",
            version="1",
            description="Qlib Alpha360: 60 sessions of raw price and volume, normalised by the latest bar.",
            features=_from_qlib_loader(alpha360_names, alpha360_expressions),
            # Genuinely selectable, not registered-then-refused: the ticket asks
            # for Alpha158 *or* Alpha360. Only the default and the wording differ,
            # because nothing shows 360 columns beat 158 on this short history.
            maturity="experimental",
        ),
        FeatureSet(
            name="momentum_only_v1",
            version="1",
            # Not a rival model but a control: ticket 06's factor, reachable
            # through the same pipeline so a difference in scores cannot be
            # blamed on a difference in plumbing.
            description="The 6-1 momentum factor of ticket 06, as a single column.",
            features=(_spec("MOM_6_1", "Ref($close, 21)/Ref($close, 147)-1"),),
            # Reachable only as the in-run control of section 9.3, never as a
            # feature set someone trains a model on.
            selectable=False,
        ),
    )
    return {item.name: item for item in sets}


def get_feature_set(name: str) -> FeatureSet:
    try:
        return _registry()[name]
    except KeyError as exc:
        raise UnknownFeatureSetError(
            f"Unknown feature set {name!r}; registered: {sorted(_registry())}"
        ) from exc


def list_feature_sets() -> list[FeatureSet]:
    return [_registry()[name] for name in sorted(_registry())]
