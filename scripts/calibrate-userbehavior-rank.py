"""Finite-support Zipf likelihood fit; never confuse log-count OLS with probability fit."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import time
import zipfile


def theoretical_mean(alpha, n):
    # Exact head and midpoint integral tail for root finding; full-support residual checked below.
    head=min(n,2048)
    z=math.fsum(i**-alpha for i in range(1,head+1))
    moment=math.fsum(i**-alpha*math.log(i) for i in range(1,head+1))
    if head<n:
        lo,hi=math.log(head+.5),math.log(n+.5)
        b=1-alpha
        if abs(b)<1e-7:
            z+=hi-lo;moment+=(hi*hi-lo*lo)/2
        else:
            z+=(math.exp(b*hi)-math.exp(b*lo))/b
            moment+=(math.exp(b*hi)*(hi/b-1/(b*b))-math.exp(b*lo)*(lo/b-1/(b*b)))
    return moment/z


def calibrate(frequencies):
    n,total=len(frequencies),sum(frequencies)
    wanted=math.fsum(f*math.log(i) for i,f in enumerate(frequencies,1))/total
    low,high=0.0,5.0
    for _ in range(60):
        alpha=(low+high)/2
        if theoretical_mean(alpha,n)>wanted:low=alpha
        else:high=alpha
    alpha=(low+high)/2
    norm=math.fsum(i**-alpha for i in range(1,n+1))
    exact_mean=math.fsum(i**-alpha*math.log(i) for i in range(1,n+1))/norm
    assert abs(exact_mean-wanted)<1e-6
    actual=predicted=gap=0.0
    for rank,count in enumerate(frequencies,1):
        actual+=count/total;predicted+=rank**-alpha/norm
        gap=max(gap,abs(actual-predicted))
    return {'zipf_exponent':alpha,'rank_cdf_max_gap':gap,'observed_mean_log_rank':wanted,
            'fitted_mean_log_rank':exact_mean,'mean_log_rank_residual':exact_mean-wanted,
            'method':'finite-support Zipf maximum likelihood via mean-log-rank equation; exact head/midpoint integral tail only for root finding, followed by exact full-support normalizer, mean and CDF checks',
            'normalizer':norm,'items':n,'events':total}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--verified',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.output.exists() or '.local' not in a.output.resolve().parts:p.error('fresh private output required')
    started=time.monotonic();f=json.loads(a.verified.read_text());counts=Counter();rows=pv=0
    h=hashlib.sha256()
    with Path(f['path']).open('rb') as stream:
        for part in iter(lambda:stream.read(1024*1024),b''):h.update(part)
    assert h.hexdigest()==f['archive_sha256']
    with zipfile.ZipFile(f['path']) as z:
        with z.open(f['members'][0]['name']) as stream:
            for raw in stream:
                fields=raw.split(b',');rows+=1
                if fields[3]==b'pv':counts[fields[1]]+=1;pv+=1
                if rows%25_000_000==0:print(json.dumps({'recount_rows':rows}),flush=True)
    assert rows==f['rows'] and pv==f['behavior_counts']['pv']
    result=calibrate(sorted(counts.values(),reverse=True))
    if 'outside_nominal_2017_11_25_to_12_03_utc' in f['source_anomalies']:
        f['source_anomalies']['outside_reference_2017_11_25_to_12_03_asia_shanghai']=f['source_anomalies'].pop('outside_nominal_2017_11_25_to_12_03_utc')
    f['reference_window_utc']=['2017-11-24T16:00:00+00:00','2017-12-03T16:00:00+00:00']
    f['reference_window_note']='explicit diagnostic window only, not proof of corruption and not a cleaning filter'
    old=f['item_popularity_pv']
    f['item_popularity_pv_ols_diagnostic']=old
    f['item_popularity_pv']={**old,**result,'top_10_share':old['top_10_share'],'log_rank_r_squared_is_ols_diagnostic_only':True}
    f['zipf_exponent']=result['zipf_exponent'];f['fit_method']=result['method']
    f['rank_calibration_seconds']=time.monotonic()-started
    f['calibration_reason']='OLS log-rank R2 does not guarantee normalized probability/CDF fidelity; do not use rejected OLS exponent for final workload'
    a.output.write_text(json.dumps(f,sort_keys=True,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result),flush=True)
