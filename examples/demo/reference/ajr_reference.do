* 开发者独立参考脚本，不注入生产模型上下文。
* AJR（2001），Table 4 第 1、2 列与 Panel C；Stata 18。
version 18
args data_path output_dir
clear all
set more off
set seed 2001
capture log close _all
log using "`output_dir'/reference.log", text replace
use "`data_path'", clear
assert _N == 163
keep if baseco == 1
assert _N == 64
assert !missing(logpgp95, avexpr, logem4, lat_abst)
tempname results diagnostics
postfile `results' str12 specification str12 estimator str16 parameter ///
    double n double coefficient double standard_error double df_r double r2 using "`output_dir'/results.dta", replace
postfile `diagnostics' str12 specification double n double instrument_coefficient ///
    double instrument_se double excluded_instrument_f double df_den using "`output_dir'/diagnostics.dta", replace
foreach specification in baseline latitude {
    local controls ""
    if "`specification'" == "latitude" local controls "lat_abst"
    quietly regress logpgp95 avexpr `controls'
    foreach parameter in avexpr `controls' _cons {
        post `results' ("`specification'") ("ols") ("`parameter'") ///
            (e(N)) (_b[`parameter']) (_se[`parameter']) (e(df_r)) (e(r2))
    }
    quietly regress avexpr logem4 `controls'
    scalar instrument_b = _b[logem4]
    scalar instrument_se = _se[logem4]
    quietly test logem4
    post `diagnostics' ("`specification'") (e(N)) (instrument_b) ///
        (instrument_se) (r(F)) (r(df_r))
    quietly ivreg logpgp95 `controls' (avexpr=logem4)
    foreach parameter in avexpr `controls' _cons {
        post `results' ("`specification'") ("ivreg") ("`parameter'") ///
            (e(N)) (_b[`parameter']) (_se[`parameter']) (e(df_r)) (e(r2))
    }
    quietly ivregress 2sls logpgp95 `controls' (avexpr=logem4), small
    foreach parameter in avexpr `controls' _cons {
        post `results' ("`specification'") ("ivregress") ("`parameter'") ///
            (e(N)) (_b[`parameter']) (_se[`parameter']) (e(df_r)) (e(r2))
    }
}
postclose `results'
postclose `diagnostics'
use "`output_dir'/results.dta", clear
format n coefficient standard_error df_r r2 %21.15g
export delimited using "`output_dir'/results.csv", datafmt replace
use "`output_dir'/diagnostics.dta", clear
format n instrument_coefficient instrument_se excluded_instrument_f df_den %21.15g
export delimited using "`output_dir'/diagnostics.csv", datafmt replace
log close
