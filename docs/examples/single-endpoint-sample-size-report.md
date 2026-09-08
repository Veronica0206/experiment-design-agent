> Documentation example, generated on 2026-09-08 from the actual local R engine with synthetic inputs. The identity and verification envelope below were constructed locally for this renderer example using deterministic design/contract checks. They are not a server-issued MCP attestation or a live-provider result. No regression-suite or replay attestation was injected; those dimensions remain “Not recorded.” The values are illustrative and do not describe a user study.

# Partially verified experiment-design result

## Question and estimand
Tool: `sample_size`

## Supplied assumptions
These are the supplied, privacy-safe arguments. Omitted engine defaults are not inferred here; any PPOS prior method below comes from the resolved engine result.
```json
{
  "alphas": [
    0.1
  ],
  "alt_param": 0.4,
  "design": "single_arm",
  "endpoint_type": "binary",
  "null_param": 0.2,
  "powers": [
    0.8
  ],
  "study_type": "poc"
}
```

## Statistical summary

Display values are rounded; the structured result below retains their full precision.

| Design | Sizing test | Alpha | Target power | Achieved power | Treatment n | Control n | Total n | Sizing status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| single_arm | exact_binomial | 0.1 | 0.8 | 0.80805476 | 24 | Unavailable | 24 | target_met |

## Assurance dimensions

| Dimension | Status |
| --- | --- |
| Calculation and result contract | Completed; required scientific metadata preserved |
| Input and result binding | Verified by the canonical reporting boundary |
| Regression suite | Not recorded |
| Same-seed replay | Not recorded |
| Statistical assumptions | Human review required; software checks do not establish suitability |
| Design performance | Not assessed by OC criteria for this deterministic sizing result. |

## Result
```json
{
  "result_contract_version": 1,
  "results": [
    {
      "alpha": 0.1,
      "design": "single_arm",
      "k_crit": 8,
      "n_ctrl": null,
      "n_total": 24,
      "n_trt": 24,
      "power_achieved": 0.8080547563197019,
      "power_target": 0.8,
      "sizing_status": "target_met",
      "test": "exact_binomial"
    }
  ]
}
```

## Verification
- verification_id: `analysis-synthetic-documentation-example`
- public_result_hash: `4549dfd3dca1040201bf4fd1b3a740c3787bb9999c3365b6ed94b97d44a67f6e`
- status: `PASS_PARTIAL`
- checks: design_validation=pass, output_contract=pass, scientific_validation=pass

## Limitations
- Boundary exactness was not automated.
- Extended configuration consistency was not automated.

## Decision
Use only the decision fields, if any, in the presentable Result block above; no additional model-generated numeric interpretation is authorized.
