# On-Route LightGBM Classifier -- Model Card

## Model Identity

- **File**: `on_route_lgbm.txt`
- **Meta**: `on_route_lgbm_meta.json`
- **Training report**: `training_report.json`
- **Version**: v4 (LightGBM binary, 2 trees, max_feature_idx=18)
- **Objective**: binary classification (sigmoid)
- **Date**: March 2026 (Valle de los Chillos pilot)

## Training Data

- **Region**: Valle de los Chillos (suburban Quito)
- **Routes used**: 22 trainable routes (route-aware train/val split)
- **Train samples**: 2,559 (430 positive, 2,129 negative)
- **Test samples**: 640 (107 positive, 533 negative)
- **Class ratio**: ~1:5 positive:negative

## Metrics

### Held-out test set (use these for production assessment)

From `training_report.json`, threshold = 0.48:

| Metric    | Value  |
|-----------|--------|
| AUC       | 0.98   |
| F1        | 0.8496 |
| Precision | 0.8067 |
| Recall    | 0.8972 |

### Validation set (inflated -- do NOT use for production assessment)

From `on_route_lgbm_meta.json`, youden_threshold = 0.4758:

| Metric    | Value  |
|-----------|--------|
| F1        | 0.9865 |
| Precision | 1.0    |
| Recall    | 0.9735 |

### F1 Discrepancy Explanation

The meta.json reports F1=0.9865 but training_report.json reports F1=0.8496. The meta
validation metrics use `youden_threshold=0.4758` evaluated on the validation fold (which
the model saw during hyperparameter tuning). The training_report metrics use
`threshold=0.48` on a held-out test set the model never saw. The training_report values
are the correct ones for production assessment. The meta values are optimistic due to
threshold tuning on the same data used to evaluate.

## Feature List (19 features)

Features as declared in the model file and `FEATURE_COLUMNS` in `on_route_classifier.py`:

| #  | Feature                            | Importance | Notes                         |
|----|------------------------------------|------------|-------------------------------|
| 0  | distance_to_corridor_m             | 566        | From training_report          |
| 1  | path_fraction                      | 589        | From training_report          |
| 2  | discovery_buffer_m                 | 0          | Zero importance               |
| 3  | operator_match                     | 0          | Zero importance               |
| 4  | cooperative_match                  | 0          | Zero importance               |
| 5  | locality_match                     | 0          | Zero importance               |
| 6  | locality_consistency_score         | --         | Not in training_report (see below) |
| 7  | bearing_alignment_deg              | --         | Not in training_report (see below) |
| 8  | is_known_anchor                    | --         | Not in training_report (see below) |
| 9  | is_known_intermediate              | --         | Not in training_report (see below) |
| 10 | stop_usage_frequency               | --         | Not in training_report (see below) |
| 11 | gap_to_previous_m                  | --         | Not in training_report (see below) |
| 12 | gap_to_next_m                      | --         | Not in training_report (see below) |
| 13 | local_stop_density                 | --         | Not in training_report (see below) |
| 14 | distance_to_envelope_m             | --         | Not in training_report (see below) |
| 15 | heuristic_score                    | 503        | From training_report          |
| 16 | in_required_area                   | --         | Appended post-training (geo)  |
| 17 | in_forbidden_area                  | --         | Appended post-training (geo)  |
| 18 | distance_to_nearest_required_area_m| --         | Appended post-training (geo)  |

### Features trained vs appended post-training

The last 3 features (indices 16-18: `in_required_area`, `in_forbidden_area`,
`distance_to_nearest_required_area_m`) were added to the feature vector after model
training as part of the geographic validation layer. They are present in the model file's
feature_names but have zero or near-zero learned splits. They are used by the ensemble
heuristic scoring, not by the LightGBM trees.

### Zero-importance features

The following features have zero importance in training_report.json, meaning the model
learned no splits on them:

- `discovery_buffer_m`
- `operator_match`
- `cooperative_match`
- `locality_match`

These are categorical/sparse features that lacked variance in the Valle de los Chillos
training data. They may become useful with urban Quito data where operator/cooperative
diversity is higher.

## Feature Name Mismatch (training_report vs model)

**CRITICAL**: The `training_report.json` feature_importance keys do NOT match the current
model's feature_names. The training_report appears to be from an earlier model version
with a different feature set:

Features in training_report.json NOT in current model:
- `gap_to_prev_m` (renamed to `gap_to_previous_m`)
- `gap_to_prev_frac`, `gap_to_next_frac` (removed)
- `local_density_200m` (renamed to `local_stop_density`)
- `route_length_km`, `route_n_stops`, `candidate_index`, `total_corridor_candidates` (removed)

Features in current model NOT in training_report:
- `locality_consistency_score`, `bearing_alignment_deg`
- `is_known_anchor`, `is_known_intermediate`
- `stop_usage_frequency`
- `distance_to_envelope_m`
- `in_required_area`, `in_forbidden_area`, `distance_to_nearest_required_area_m`

This means the training_report.json metrics (AUC=0.98, F1=0.8496) were measured on a
DIFFERENT model than the one currently deployed in `on_route_lgbm.txt`. The current model
was retrained with the 19-feature schema but no updated training_report was generated.
The meta.json validation metrics (F1=0.9865) are from the current model but on the
validation fold, not a held-out test set.

## Ensemble Weights

The classifier uses an 80/20 heuristic/LightGBM ensemble:

- **Heuristic weight**: 0.80
- **LightGBM weight**: 0.20

This conservative weighting was chosen because the model showed signs of overfitting
(AUC >= 0.99 on validation). The heuristic-dominant ensemble prevents the model from
overriding well-tuned domain rules.

## Thresholds

| Threshold        | Value  | Purpose                              |
|------------------|--------|--------------------------------------|
| on_route         | 0.48   | Ensemble score >= this = ON_ROUTE    |
| marginal         | 0.3072 | Score in [marginal, on_route) = MARGINAL |
| youden_threshold | 0.4758 | Optimal threshold from ROC (val set) |

Note: `on_route_classifier.py` uses slightly different defaults (0.50 / 0.32) which
may diverge from the meta.json values (0.48 / 0.3072).

## Scaling Notes

This model was trained exclusively on Valle de los Chillos routes (suburban, ~22 routes).
It will NOT generalize well to:

- **Urban Quito**: Different stop density, naming patterns, operator diversity
- **Inter-cantonal routes**: Longer distances, highway stops
- **BRT/Metrobus corridors**: Fixed infrastructure, different stop patterns

Retraining is required before applying to these contexts. The 80/20 ensemble weighting
partially mitigates this by relying mostly on the heuristic, but production use outside
Valle de los Chillos should be validated carefully.
