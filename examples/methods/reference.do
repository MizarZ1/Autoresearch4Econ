* 独立原生 Stata 参考：不调用生产提取脚本，不注入模型上下文。
* 自编合成数据，所有观测为数值验收用途，不代表真实政策或研究结论。
version 18
args input_path output_dir
clear all
set more off
set processors 1
set seed 2001
import delimited using "`input_path'", varnames(1) clear
log using "`output_dir'/native_reference.log", text replace
foreach model in linear did rdd logit {
    if "`model'" == "linear" regress ylinear x if included==1
    if "`model'" == "did" regress ydid group post did if included==1, vce(robust)
    if "`model'" == "rdd" regress yrdd treated centered interaction if abs(running-0)<=0.5, vce(robust)
    if "`model'" == "logit" logit binary x if included==1
    local names : colnames e(b)
    scalar ref_n = e(N)
    tempname ref_file
    postfile `ref_file' str32 parameter double coefficient double standard_error double p_value double lower double upper double df double n using "`output_dir'/`model'.dta", replace
    foreach parameter of local names {
        quietly lincom `parameter', level(95)
        post `ref_file' ("`parameter'") (r(estimate)) (r(se)) (r(p)) (r(lb)) (r(ub)) (r(df)) (ref_n)
    }
    postclose `ref_file'
}
summarize x if included==1
tempname ref_summary
postfile `ref_summary' double N double mean double sd double min double max using "`output_dir'/summary.dta", replace
post `ref_summary' (r(N)) (r(mean)) (r(sd)) (r(min)) (r(max))
postclose `ref_summary'
foreach model in linear did rdd logit summary {
    preserve
    use "`output_dir'/`model'.dta", clear
    ds, has(type numeric)
    format `r(varlist)' %24.17g
    export delimited using "`output_dir'/`model'.csv", datafmt replace
    restore
}
log close
