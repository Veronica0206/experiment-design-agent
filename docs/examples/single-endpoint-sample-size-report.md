# Partially verified experiment-design result

## Question and estimand
Tool: `sample_size`

## Resolved assumptions
```json
{
  "alphas": 0.1,
  "alt_param": 0.4,
  "design": "single_arm",
  "endpoint_type": "binary",
  "null_param": 0.2,
  "powers": 0.8,
  "study_type": "poc"
}
```

## Result
```json
{
  "results": [
    {
      "alpha": 0.1,
      "design": "single_arm",
      "k_crit": 8,
      "n_ctrl": null,
      "n_total": 24,
      "n_trt": 24,
      "power_achieved": 0.8081,
      "power_target": 0.8,
      "test": "exact_binomial"
    }
  ]
}
```

## Verification
- verification_id: `analysis-4c9a5918-053d-4d9a-9edc-a787fdf4e10d`
- public_result_hash: `c453c0d172ee6281143c622d28697c7c7f70926dee0e70fbf10a76f0bde7937f`
- status: `PASS_PARTIAL`
- checks: design_validation=pass, output_contract=pass, regression_suite=pass, scientific_validation=pass

## Limitations
- Boundary exactness was not automated.
- Extended configuration consistency was not automated.

## Decision
Use only the decision fields, if any, in the presentable Result block above; no additional model-generated numeric interpretation is authorized.