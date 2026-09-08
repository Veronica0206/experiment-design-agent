#!/usr/bin/env Rscript
# Cross-path regressions for analysis-test contracts and integer design outputs.
script <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE)[1])
if (!file.exists(script)) script <- gsub("~+~", " ", script, fixed = TRUE)
r_dir <- normalizePath(file.path(dirname(script), "..", "R"))
source(file.path(r_dir, "run_framework.R"))
passed <- failed <- 0L
test <- function(name, expression) {
  tryCatch({
    force(expression)
    cat("TEST", name, ": PASS\n")
    passed <<- passed + 1L
  }, error = function(e) {
    cat("TEST", name, ": FAIL", conditionMessage(e), "\n")
    failed <<- failed + 1L
  })
}
near <- function(a, b, tolerance = 1e-9) stopifnot(max(abs(a-b)) < tolerance)

test("binary_sizing_and_ppos_share_unpooled_rejection_rule", {
  cfg <- create_config(endpoint_type="binary", study_type="confirmatory",
    design="controlled", null_param=.1, alt_param=.3, alloc_ratio=2,
    alphas=.025, powers=.8, p3_n=117, p3_alloc_ratio=2,
    p2_data=list(x=30,n=100), p2_data_ctrl=list(x=10,n=100))
  sizes <- compute_sample_size(cfg)
  row <- sizes[sizes$design == "controlled", ]
  stopifnot(row$n_trt == 78, row$n_ctrl == 39, row$test == "z_unpooled")
  near(row$power_achieved, pnorm(.2/sqrt(.3*.7/78+.1*.9/39)-qnorm(.975)))
  set.seed(771)
  result <- ppos_binary_two_arm(cfg, n_mc=512)
  set.seed(771)
  treatment <- rbeta(512,30.5,70.5); control <- rbeta(512,10.5,90.5)
  expected <- pnorm((treatment-control)/sqrt(treatment*(1-treatment)/78+
                        control*(1-control)/39)-qnorm(.975))
  stopifnot(result$confirmatory_test == row$test)
  near(result$cond_power_draws, expected)
})

# Independent integration over the chi distribution (the implementation
# instead conditions on the normal numerator for its numerical fallback).
reference_tail <- function(q, df, ncp) {
  integrate(function(s) pnorm(ncp-q*s) * 2*df*s*dchisq(df*s*s,df),
            0, Inf, rel.tol=1e-10, abs.tol=1e-11, subdivisions=1000)$value
}
test("noncentral_t_extreme_low_df_counterexample", {
  critical <- qt(.001,1,lower.tail=FALSE)
  value <- noncentral_t_upper_tail(critical,1,40)
  near(value, reference_tail(critical,1,40), 1e-9)
  stopifnot(value > .0999, value < .1001)
})
test("noncentral_t_full_parameter_extreme_grid", {
  for (df in c(1,2,5,100)) for (alpha in c(.001,.025,.25)) {
    critical <- qt(alpha,df,lower.tail=FALSE)
    for (ncp in c(-40,40,75)) {
      near(noncentral_t_upper_tail(critical,df,ncp),
           reference_tail(critical,df,ncp), 1e-8)
    }
  }
  near(noncentral_t_upper_tail(0,1,40),pnorm(40))
  near(noncentral_t_upper_tail(-3,2,40),1-reference_tail(3,2,-40))
})
test("noncentral_t_moderate_region_agrees_with_R", {
  for (n in c(2,5,20,100)) for (delta in c(.1,.5,1.5)) {
    expected <- power.t.test(n=n,delta=delta,sd=1,sig.level=.025,
                            type="one.sample",alternative="one.sided")$power
    near(power_ttest_single_arm(n,delta,1),expected,1e-9)
  }
})
test("continuous_single_ppos_does_not_force_large_ncp_to_one", {
  cfg <- create_config(endpoint_type="continuous",study_type="confirmatory",
    design="single_arm",null_param=0,alt_param=1,sd=1,
    p3_n=2,p3_alpha=.001,p2_data=list(n=100000,x_bar=sqrt(800),s2=1))
  set.seed(481)
  result <- compute_ppos(cfg,n_mc=100)
  stopifnot(result$ppos > .095,result$ppos < .105,
            result$confirmatory_test == "one_sample_noncentral_t")
})
test("continuous_two_arm_ppos_does_not_force_large_ncp_to_one", {
  cfg <- create_config(endpoint_type="continuous",study_type="confirmatory",
    design="controlled",null_param=0,alt_param=1,sd=1,
    p3_n=4,p3_alpha=.001,p2_data=list(n=100000,x_bar=40,s2=1),
    p2_data_ctrl=list(n=100000,x_bar=0,s2=1))
  set.seed(482)
  result <- compute_ppos(cfg,n_mc=100)
  stopifnot(result$ppos > .95,result$ppos < .97,
            result$confirmatory_test == "welch_noncentral_t_approximation")
})
test("continuous_integer_sizing_minima_and_actual_power", {
  for (ratio in c(1,1.01,2,9)) for (delta in c(.4,10,100)) {
    cfg <- create_config(endpoint_type="continuous",study_type="confirmatory",
      design="controlled",null_param=0,alt_param=delta,sd=1,alloc_ratio=ratio,
      alphas=.025,powers=.8)
    rows <- compute_sample_size(cfg)
    stopifnot(all(rows$n_trt >= 2),all(rows$n_ctrl[rows$design=="controlled"] >= 2),
              all(rows$power_achieved >= rows$power_target),
              all(rows$sizing_status == "target_met"))
    for(i in seq_len(nrow(rows))) {
      row <- rows[i,]
      expected <- if(row$design=="single_arm") power_ttest_single_arm(row$n_total,delta,1) else
        power_ttest_two_arm(row$n_trt,row$n_ctrl,delta,1)
      near(row$power_achieved,expected)
      if(row$design=="controlled") stopifnot(row$test ==
        if(row$n_trt==row$n_ctrl) "two_sample_t" else "two_sample_z_normal_approximation")
    }
    stopifnot(any(rows$power_achieved != rows$power_target))
  }
})
test("tte_integer_allocation_uses_achieved_information", {
  for(ratio in c(1,2,20)) {
    cfg <- create_config(endpoint_type="tte",study_type="confirmatory",
      design="controlled",null_param=.2,alt_param=.1,alloc_ratio=ratio,
      accrual_time=12,followup_time=6,alphas=.025,powers=.8)
    rows <- compute_sample_size(cfg)
    stopifnot(all(rows$n_trt >= 1),all(rows$n_ctrl[rows$design=="controlled"] >= 1),
              all(rows$power_achieved >= .8),all(rows$power_achieved != .8))
    for(i in seq_len(nrow(rows))) {
      row <- rows[i,]
      expected <- if(row$design=="single_arm") power_logrank_single_arm(row$n_total,.2,.1,12,6) else
        power_logrank_two_arm(row$n_trt,row$n_ctrl,.2,.1,12,6)
      near(row$power_achieved,expected)
    }
  }
})
test("unattainable_search_is_explicit", {
  cfg <- create_config(endpoint_type="binary",study_type="confirmatory",
    design="single_arm",null_param=.2,alt_param=.20001,alphas=.025,powers=.8)
  row <- compute_sample_size(cfg)
  stopifnot(is.na(row$n_total),is.na(row$power_achieved),
            row$sizing_status == "search_limit_reached")
})
test("ppos_ratio_moment_existence_is_explicit", {
  for(endpoint in c("tte","incidence_rate")) for(control_events in c(0,2)) {
    fields <- if(endpoint=="tte") list(accrual_time=12,followup_time=6,
      p2_data=list(events=3,person_time=100),
      p2_data_ctrl=list(events=control_events,person_time=100)) else
      list(exposure_time=1,p2_data=list(count=3,exposure=100),
           p2_data_ctrl=list(count=control_events,exposure=100))
    cfg <- do.call(create_config,c(list(endpoint_type=endpoint,study_type="confirmatory",
      design="controlled",null_param=.2,alt_param=.1,p3_n=100),fields))
    set.seed(900)
    result <- compute_ppos(cfg,n_mc=200)
    prefix <- if(endpoint=="tte") "hr" else "rr"
    status <- result[[paste0(prefix,"_post_mean_status")]]
    value <- result[[paste0(prefix,"_post_mean")]]
    if(control_events==0) stopifnot(is.na(value),status=="does_not_exist") else {
      near(value,3.5/1.5)
      stopifnot(status=="finite")
    }
    stopifnot(is.finite(result[[paste0(prefix,"_post_median")]]),
      all(is.finite(result[[paste0(prefix,"_post_ci")]])),
      result$ratio_summary_method=="scaled_beta_prime",is.finite(result$ppos))
  }
})
cat("Planning consistency:",passed,"passed;",failed,"failed\n")
quit(status=if(failed) 1 else 0)
