---
status: accepted
date: 2026-09-07
---

# Cache experiment features as per-security shards keyed by data identity

The direct experiment entry point reads a QlibDataBundle for prices, as ADR-0001 requires, but computes Alpha158/Alpha360 features from it rather than storing them there — a bundle holds market facts, not derived features. It did so in one call: Qlib returned the whole market, `DataHandlerLP` kept two processed copies of it, and an Alpha360 run over ~4700 securities was killed before it finished. We chose to compute features in security-sized batches into a checksummed FeatureShardCache and stream each segment back into one preallocated matrix, instead of raising the memory ceiling or reducing the universe, because the peak then scales with the segment being trained on rather than with the experiment; the accepted cost is a second derived on-disk representation alongside QlibDataBundle, and a hand-written equivalent of three Qlib processors.

Sharding by security rather than by date is forced rather than chosen: a forward label is computed with `shift` along each security's own series, and cutting that series by date would truncate the label near every boundary.

## Consequences

- A FeatureShardCache holds derived features and labels for one experiment span, computed from a QlibDataBundle rather than replacing it. ADR-0001 stands: the bundle remains the only thing Qlib reads market facts from. Like a bundle, this is not a fact source, is always rebuildable, and is safe to delete.
- Its identity is a function of the data only — DataSnapshot, QlibDataBundle checksum, FeatureSet definition, label horizon, stock pool policy, date span. Not of the code, so an unrelated commit does not discard it; not of the batch sizes, which divide the work without changing a cached value.
- Fold geometry must not reach the cached rows. The stock pool is therefore built for the whole span rather than for the days some fold layout happens to touch, and `FeatureCacheSpec` is a separate type from the run configuration so that a new knob cannot silently widen what a cache is keyed on. A cache refuses to seal unless every day holds exactly as many rows as the pool named for it — presence per day is not completeness per day, and features and labels shorten together, so nothing downstream can notice. The days a run needs are an argument to assembly rather than a question asked of the reader afterwards. Under the current design that check cannot fail; it is there as an executable statement of the invariant, so that reintroducing a fold-derived pool fails loudly.
- `InfToNaN`, `DropnaLabel` and `CSRankNorm` are applied by this module rather than by `DataHandlerLP`. All three are stateless, so precomputing them fits no window and leaks nothing, but the equivalence is an acceptance test against a real Qlib pipeline rather than a claim.
- Features are stored as float32. Numbers therefore differ from the float64 `DataHandlerLP` path this replaced, and any frozen baseline captured under it must be re-frozen.
- Trials run serially. Each loads its own segments, so concurrency would raise the peak this decision exists to lower.
