# Partially verified experiment-design result

## Question and estimand
Tool: `ab_test`

## Resolved assumptions
```json
{
  "alpha": 0.05,
  "baseline": 0.1,
  "effect": 0.02,
  "effect_type": "absolute",
  "metric": "proportion",
  "power": 0.8,
  "ratio": 1,
  "sided": 2
}
```

## Result
```json
{
  "achieved_power": 0.8001,
  "allocation_ratio": 1,
  "alpha": 0.05,
  "baseline": 0.1,
  "mde": 0.02,
  "metric": "proportion",
  "n_control": 3839,
  "n_total": 7678,
  "n_treatment": 3839,
  "sided": 2,
  "target_power": 0.8
}
```

## Verification
- verification_id: `analysis-09dfd289-7111-468c-9ef6-ce754f4190bb`
- public_result_hash: `8c3ce525bb358ac6afb82c4f76fb7dfd93a58544ae356c1fd076669974c28e33`
- status: `PASS_PARTIAL`
- checks: output_contract=pass, regression_suite=pass, scientific_validation=pass

## Limitations
- Boundary exactness was not automated.
- Extended configuration consistency was not automated.

## Decision
Use only the decision fields, if any, in the presentable Result block above; no additional model-generated numeric interpretation is authorized.