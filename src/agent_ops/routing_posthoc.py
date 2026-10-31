"""Offline paired inference on stored predictions; no router or provider execution."""
import math
import random
from collections import defaultdict
from itertools import combinations
from typing import Any

LABELS=('order_status','return_request','human','clarify')
VERSIONS=('rule','model','hybrid')


def exact_mcnemar(left_only: int, right_only: int) -> float:
    n=left_only+right_only
    if min(left_only,right_only)<0:raise ValueError('NEGATIVE_DISCORDANCE')
    if not n:return 1.
    return float(min(1.,2*sum(math.comb(n,k) for k in range(min(left_only,right_only)+1))/(2**n)))


def holm_adjust(values: list[float]) -> list[float]:
    result=[0.]*len(values);previous=0.
    for rank,index in enumerate(sorted(range(len(values)),key=values.__getitem__)):
        previous=max(previous,min(1.,values[index]*(len(values)-rank)));result[index]=previous
    return result


def percentile(values: list[float], q: float) -> float:
    values=sorted(values);position=(len(values)-1)*q;low=int(position);high=math.ceil(position)
    return values[low]+(values[high]-values[low])*(position-low)


def macro_f1(gold: list[str], predicted: list[str | None], labels: tuple[str,...]) -> float:
    if len(gold)!=len(predicted) or not gold:raise ValueError('EMPTY_OR_MISMATCHED_SAMPLE')
    scores=[]
    for label in labels:
        tp=sum(g==p==label for g,p in zip(gold,predicted,strict=True))
        denominator=sum(g==label for g in gold)+sum(p==label for p in predicted)
        scores.append(2*tp/denominator if denominator else 0.)
    return sum(scores)/len(labels)


def paired_analysis(rows: list[dict[str,Any]], *, replicates: int, seed: int,
                    exclude_gold_human: bool) -> dict[str,Any]:
    valid=[r for r in rows if r['label'] is not None and (not exclude_gold_human or r['label']!='human')]
    labels=tuple(x for x in LABELS if not exclude_gold_human or x!='human')
    if not valid or replicates<100:raise ValueError('INSUFFICIENT_BOOTSTRAP_CONFIGURATION')
    gold=[r['label'] for r in valid];pred={v:[r[v] for r in valid] for v in VERSIONS}
    strata: dict[str,list[int]]=defaultdict(list)
    for i,label in enumerate(gold):strata[label].append(i)
    rng=random.Random(seed);distributions: dict[str,list[float]]={v:[] for v in VERSIONS}
    alternative: dict[str,list[float]]={v:[] for v in VERSIONS}
    three=tuple(x for x in LABELS if x!='human')
    for _ in range(replicates):
        indices=[i for group in strata.values() for i in rng.choices(group,k=len(group))]
        sampled_gold=[gold[i] for i in indices]
        for v in VERSIONS:
            sampled_pred=[pred[v][i] for i in indices]
            distributions[v].append(macro_f1(sampled_gold,sampled_pred,labels))
            if not exclude_gold_human:alternative[v].append(macro_f1(sampled_gold,sampled_pred,three))
    report: dict[str,Any]={'n':len(valid),'macro_labels':list(labels),'exclude_gold_human':exclude_gold_human,
      'replicates':replicates,'seed':seed,'sampling':'paired stratified bootstrap; fixed human-gold class counts',
      'zero_denominator_f1':0,'versions':{},'pairs':[]}
    for v in VERSIONS:
        report['versions'][v]={'macro_f1':macro_f1(gold,pred[v],labels),'ci95':[percentile(distributions[v],.025),percentile(distributions[v],.975)]}
        if not exclude_gold_human:
            report['versions'][v]['three_macro_all_rows_alternative']={
                'macro_f1':macro_f1(gold,pred[v],three),'ci95':[percentile(alternative[v],.025),percentile(alternative[v],.975)]}
    # 12 intervals = 2 sources x 2 populations x 3 pairs. Bonferroni nominal family coverage 95% (bootstrap approximation).
    tail=.05/(2*12)
    for a,b in combinations(VERSIONS,2):
        left=sum(g==pa and g!=pb for g,pa,pb in zip(gold,pred[a],pred[b],strict=True))
        right=sum(g!=pa and g==pb for g,pa,pb in zip(gold,pred[a],pred[b],strict=True))
        diffs=[x-y for x,y in zip(distributions[a],distributions[b],strict=True)]
        report['pairs'].append({'a':a,'b':b,'a_only_correct':left,'b_only_correct':right,
            'accuracy_difference_a_minus_b':(left-right)/len(valid),'mcnemar_exact_two_sided_p':exact_mcnemar(left,right),
            'macro_difference_a_minus_b':report['versions'][a]['macro_f1']-report['versions'][b]['macro_f1'],
            'paired_bootstrap_ci95':[percentile(diffs,.025),percentile(diffs,.975)],
            'paired_bootstrap_family_ci':[percentile(diffs,tail),percentile(diffs,1-tail)],
            'family_confidence':1-2*tail,'family_comparisons':12})
    return report
