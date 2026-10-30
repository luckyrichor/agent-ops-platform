"""Read the private human workbook and publish only gold metrics, IDs and diagnostic categories."""
import argparse
import hashlib
import json
import os
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook

from agent_ops.p8_pilot import private_write
from agent_ops.routing_metrics import LABELS, group_metrics, propose_clarify_subtype


def read_rows(annotated,original,dataset,predictions):
    book=load_workbook(annotated,data_only=False,keep_links=False)
    original_book=load_workbook(original,data_only=False,keep_links=False)
    sheet=book['02_盲标100条'];before=original_book['02_盲标100条']
    if sheet.max_row!=101 or sheet.max_column!=8:raise ValueError('EXPECTED_100_ANNOTATION_ROWS')
    source={r['id']:r for r in json.loads(dataset.read_text())['samples']}
    predicted={r['id']:r for r in json.loads(predictions.read_text())}
    result=[];seen=set()
    for i in range(2,102):
        row=[sheet.cell(i,k).value for k in range(1,9)]
        if any(sheet.cell(i,k).data_type=='f' for k in range(1,9)):raise ValueError('FORMULA_NOT_ACCEPTED')
        if row[:5]!=[before.cell(i,k).value for k in range(1,6)]:raise ValueError('BLIND_CONTENT_MODIFIED')
        item_id=row[0]
        if item_id in seen or item_id not in source or item_id not in predicted:raise ValueError('INVALID_OR_DUPLICATE_ID')
        seen.add(item_id)
        label=(row[6].strip() or None) if isinstance(row[6],str) else None
        if label not in (*LABELS,None):raise ValueError('UNKNOWN_HUMAN_LABEL')
        role=row[5]
        if role!='顾客' and label is not None:raise ValueError('NONCUSTOMER_GOLD_NOT_ACCEPTED')
        result.append({'id':item_id,'source':source[item_id]['source'],'gold':label,'role':role,
          'note':row[7] or '',**{v:predicted[item_id][v] for v in ['rule','model','hybrid']}})
    return result


def analyze(args):
    rows=read_rows(args.annotated,args.original,args.dataset,args.predictions)
    # Source notes/text remain private; diagnostic public rows only have IDs/categories/labels.
    private_write(args.private_output/'human-joined.json',rows)
    groups=group_metrics(rows)
    proposal=[]
    disagreements=[]
    for row in rows:
        if row['gold']=='clarify':
            subtype,basis=propose_clarify_subtype(row['note'])
            proposal.append({'id':row['id'],'source':row['source'],'proposed_subtype':subtype,'basis':basis})
        if row['gold'] is None:continue
        wrong=[v for v in ['rule','model','hybrid'] if row[v]!=row['gold']]
        if not wrong:continue
        note=row['note']
        if any(w in note for w in ['否定','引用']):cause='negation_or_quotation'
        elif any(w in note for w in ['主次','多意图','多个','混合']):cause='multiple_intents_or_no_main_intent'
        elif row['gold']=='clarify':
            cause='clarify_vs_executable_action_boundary'
        elif row['gold']=='human' or any(row[v]=='human' for v in wrong):cause='human_escalation_boundary'
        else:cause='context_or_business_action_interpretation'
        # Risk ordering is explicit; it is a review priority, not a model confidence score.
        severity=3 if row['gold']=='human' or any({row[v],row['gold']}=={'order_status','return_request'} for v in wrong) else 2 if row['gold']=='clarify' else 1
        disagreements.append({'id':row['id'],'source':row['source'],'gold':row['gold'],
          **{v:row[v] for v in ['rule','model','hybrid']},'wrong_versions':wrong,
          'review_priority':severity,'proposed_cause':cause})
    disagreements.sort(key=lambda r:(-len(r['wrong_versions']),-r['review_priority'],r['id']))
    # Includes all 3 abstentions in coverage/report, never calls them model errors.
    result={'stage':'development gold only; no final-test metrics','human_file_sha256':hashlib.sha256(args.annotated.read_bytes()).hexdigest(),
      'candidate_prediction_source_commit':'0bb74bd','rule_prompt_threshold_changed_for_this_analysis':False,
      'additional_model_calls':0,'groups':groups,'disagreements':disagreements,
      'disagreement_causes':dict(Counter(r['proposed_cause'] for r in disagreements)),
      'clarify_subtype_proposal':{'status':'awaiting_owner_confirmation; not applied to reported main-label metrics',
        'counts':dict(Counter(r['proposed_subtype'] for r in proposal)),
        'by_source':{s:dict(Counter(r['proposed_subtype'] for r in proposal if r['source']==s)) for s in ['p2_synthetic','ecd_redacted']},
        'rules':{'out_of_scope':'notes mention scope/pre-sales/bargaining/gifts/discounts/product queries/quantity/specifications',
                 'social_ack':'notes mention thanks/chat/closing/acknowledgement or no-new-action order/plan notifications',
                 'insufficient_or_ambiguous':'remaining clarify notes: insufficient request, negative-only, ambiguity or no main intent'},
        'boundary_ids':['ecd-0207','ecd-0313','ecd-0560','ecd-0370','ecd-0152'],
        'note':'proposed diagnostic taxonomy only, root gold unchanged; confirmation before use in formal reporting'}}
    args.public_output.mkdir(parents=True,exist_ok=True)
    (args.public_output/'metrics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    private_write(args.private_output/'clarify-proposed-mapping.json',proposal)
    print(json.dumps({'groups':{g:{'n':v['labeled_rows'],'abstentions':v['abstentions'],
      'majority':v['majority_baseline']['accuracy'],'versions':{k:{'accuracy':x['accuracy'],'macro_f1_fixed_four':x['macro_f1_fixed_four'],'kappa':x['cohen_kappa']} for k,x in v['versions'].items()}} for g,v in groups.items()},
      'proposed_clarify_subtypes':result['clarify_subtype_proposal']['counts'],'disagreement_causes':result['disagreement_causes']},ensure_ascii=False))


def main():
    p=argparse.ArgumentParser(__doc__)
    p.add_argument('--annotated',type=Path,required=True);p.add_argument('--original',type=Path,required=True)
    p.add_argument('--dataset',type=Path,required=True);p.add_argument('--predictions',type=Path,required=True)
    p.add_argument('--private-output',type=Path,required=True);p.add_argument('--public-output',type=Path,required=True)
    args=p.parse_args();os.umask(0o077);root=(Path.cwd()/'.local').resolve()
    for path in [args.annotated,args.original,args.dataset,args.predictions,args.private_output]:
        if path.is_symlink() or not path.resolve().is_relative_to(root):p.error('annotation/raw inputs require private .local paths')
    analyze(args)


if __name__=='__main__':
    try:main()
    except Exception as e:  # noqa: BLE001 -- keep annotation text/notes out of errors
        print(json.dumps({'analysis_failed_type':type(e).__name__}));raise SystemExit(1) from None
