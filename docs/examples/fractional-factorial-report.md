# Partially verified experiment-design result

## Question and estimand
Tool: `factorial_design`

## Resolved assumptions
```json
{
  "center_points": 0,
  "fraction": 1,
  "levels": 2,
  "n_factors": 4,
  "randomize": false,
  "replicates": 1
}
```

## Result
```json
{
  "design": [
    {
      "factor_1": -1,
      "factor_2": -1,
      "factor_3": -1,
      "factor_4": -1
    },
    {
      "factor_1": 1,
      "factor_2": -1,
      "factor_3": -1,
      "factor_4": 1
    },
    {
      "factor_1": -1,
      "factor_2": 1,
      "factor_3": -1,
      "factor_4": 1
    },
    {
      "factor_1": 1,
      "factor_2": 1,
      "factor_3": -1,
      "factor_4": -1
    },
    {
      "factor_1": -1,
      "factor_2": -1,
      "factor_3": 1,
      "factor_4": 1
    },
    {
      "factor_1": 1,
      "factor_2": -1,
      "factor_3": 1,
      "factor_4": -1
    },
    {
      "factor_1": -1,
      "factor_2": 1,
      "factor_3": 1,
      "factor_4": -1
    },
    {
      "factor_1": 1,
      "factor_2": 1,
      "factor_3": 1,
      "factor_4": 1
    }
  ],
  "n_factors": 4,
  "n_runs": 8,
  "replicates": 1,
  "resolution": 4,
  "type": "fractional_factorial"
}
```

## Verification
- verification_id: `analysis-2e586760-0c42-4721-9ae0-3715a9681dd6`
- public_result_hash: `5dd63b4accffa3b5722a4291b80bb97bfcd57aeacfb2c14ed21968bbd6bae462`
- status: `PASS_PARTIAL`
- checks: design_validation=pass, output_contract=pass, regression_suite=pass, scientific_validation=pass

## Limitations
- Boundary exactness was not automated.
- Extended configuration consistency was not automated.

## Decision
Use only the decision fields, if any, in the presentable Result block above; no additional model-generated numeric interpretation is authorized.