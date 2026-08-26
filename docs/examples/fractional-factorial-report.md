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
  "alias_structure": {
    "classes": [
      {
        "effects": [
          [
            1
          ]
        ]
      },
      {
        "effects": [
          [
            2
          ]
        ]
      },
      {
        "effects": [
          [
            3
          ]
        ]
      },
      {
        "effects": [
          [
            4
          ]
        ]
      },
      {
        "effects": [
          [
            1,
            2
          ],
          [
            3,
            4
          ]
        ]
      },
      {
        "effects": [
          [
            1,
            3
          ],
          [
            2,
            4
          ]
        ]
      },
      {
        "effects": [
          [
            1,
            4
          ],
          [
            2,
            3
          ]
        ]
      }
    ],
    "factor_index_basis": "one_based_public_design_columns",
    "scope": "main_and_two_factor"
  },
  "defining_relation": [
    [
      1,
      2,
      3,
      4
    ]
  ],
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
  "generators": [
    {
      "generated_factor_index": 4,
      "source_factor_indices": [
        1,
        2,
        3
      ]
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
- verification_id: `analysis-8e3f2f2e-848c-4a9f-8486-9cb01c6dfa56`
- public_result_hash: `b740b80550fb13fd2d07d920d9d6df0d01a694f0490ac0fb3ad2c00fd943327e`
- status: `PASS_PARTIAL`
- checks: design_validation=pass, output_contract=pass, regression_suite=pass, scientific_validation=pass

## Limitations
- Boundary exactness was not automated.
- Extended configuration consistency was not automated.

## Decision
Use only the decision fields, if any, in the presentable Result block above; no additional model-generated numeric interpretation is authorized.
