"""Offline human-gold development metrics with explicit abstentions and fixed label order."""
from collections import Counter
from collections.abc import Sequence
from math import sqrt
from typing import Any

LABELS=('order_status','return_request','human','clarify')


def wilson_interval(successes: int, total: int) -> dict[str,Any]:
    if not 0 <= successes <= total:
        raise ValueError('INVALID_BINOMIAL_COUNTS')
    if not total:
        return {'successes':successes,'n':0,'confidence':.95,'low':None,'high':None}
    z=1.959963984540054
    p=successes/total;denom=1+z*z/total
    center=(p+z*z/(2*total))/denom
    half=z*sqrt(p*(1-p)/total+z*z/(4*total*total))/denom
    return {'successes':successes,'n':total,'confidence':.95,
            'low':max(0,center-half),'high':min(1,center+half)}


def classification_metrics(gold: Sequence[str], predicted: Sequence[str | None]) -> dict[str,Any]:
    if len(gold)!=len(predicted) or any(x not in LABELS for x in gold) or any(x is not None and x not in LABELS for x in predicted):
        raise ValueError('INVALID_LABELS_OR_LENGTH')
    prediction_labels: list[str | None]=[*LABELS]+([None] if None in predicted else [])
    matrix=[[0 for _ in prediction_labels] for _ in LABELS]
    for true,pred in zip(gold,predicted,strict=True):matrix[LABELS.index(true)][prediction_labels.index(pred)]+=1
    n=len(gold);support=Counter(gold);prediction=Counter(predicted)
    correct=sum(matrix[i][i] for i in range(4))
    per_class: dict[str,Any]={}
    for i,label in enumerate(LABELS):
        tp=matrix[i][i];fp=prediction[label]-tp;fn=support[label]-tp
        per_class[label]={'support':support[label],'predicted':prediction[label],
          'precision':tp/(tp+fp) if tp+fp else None,'recall':tp/(tp+fn) if tp+fn else None,
          'f1':2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None,
          'precision_wilson':wilson_interval(tp,tp+fp),'recall_wilson':wilson_interval(tp,tp+fn)}
    expected=sum(support[x]*prediction[x] for x in LABELS)/(n*n) if n else None
    accuracy=correct/n if n else None
    kappa=(accuracy-expected)/(1-expected) if expected is not None and expected<1 and accuracy is not None else None
    return {'n':n,'prediction_failures':sum(x is None for x in predicted),'prediction_label_order':[x or 'FAILED' for x in prediction_labels],'label_order':list(LABELS),'accuracy':accuracy,'cohen_kappa':kappa,
      'kappa_kind':'human versus router/model; not two human annotators','per_class':per_class,
      'macro_f1_fixed_four':sum(v['f1'] or 0 for v in per_class.values())/4 if n else None,
      'macro_f1_present_gold':sum(per_class[x]['f1'] or 0 for x in LABELS if support[x])/sum(support[x]>0 for x in LABELS) if n else None,
      'confusion_matrix_rows_gold_cols_prediction':matrix,
      'undefined_policy':'P/R/F1 undefined shown null; fixed-four macro treats undefined as 0; present-gold macro additionally shown'}


def group_metrics(rows: list[dict[str,Any]]) -> dict[str,Any]:
    groups={}
    for name,source in [('p2_synthetic','p2_synthetic'),('ecd_redacted','ecd_redacted'),('combined',None)]:
        selected=[r for r in rows if source is None or r['source']==source]
        valid=[r for r in selected if r['gold'] is not None]
        gold=[r['gold'] for r in valid];counts=Counter(gold)
        maximum=max(counts.values(),default=0)
        ties=[x for x in LABELS if counts[x]==maximum] if valid else []
        majority=ties[0] if ties else None
        result: dict[str,Any]={'total_rows':len(selected),'labeled_rows':len(valid),'abstentions':len(selected)-len(valid),
          'abstention_ids':[r['id'] for r in selected if r['gold'] is None],
          'coverage':len(valid)/len(selected) if selected else None,'gold_counts':dict(counts),
          'majority_baseline':{'tied_classes':ties,'selected_class_fixed_order':majority,
            'accuracy':maximum/len(valid) if valid else None,
            'description':'descriptive development majority, not a fitted final-test classifier'},'versions':{}}
        if majority is not None:
            result['majority_baseline']['metrics']=classification_metrics(gold,[majority]*len(valid))
        for version in ['rule','model','hybrid']:
            predicted=[r[version] for r in valid]
            result['versions'][version]=classification_metrics(gold,predicted)
        groups[name]=result
    return groups


def propose_clarify_subtype(note: str) -> tuple[str,str]:
    # Diagnostic proposal from existing annotation notes; never alters root gold labels.
    if any(w in note for w in ['超范围','售前','议价','赠品','优惠','咨询','规格','材质','尺寸','购买数量']):
        return 'out_of_scope','annotation_scope_or_product_terms'
    if any(w in note for w in ['致谢','闲聊','收尾','回应','确认','告知已拍','下单告知','明天再买']):
        return 'no_action','annotation_social_or_ack_terms'
    return 'insufficient_or_ambiguous','no_executable_request_or_unresolved_main_intent'
