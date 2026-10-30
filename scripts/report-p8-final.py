"""Offline presentation of the completed single final run; never calls a provider."""
import hashlib
import json
from collections import Counter
from pathlib import Path

from agent_ops.p8_release import verify_freeze
from agent_ops.routing_metrics import LABELS, classification_metrics


def main():
    root=Path('.local/p8-final-release')
    public=Path('docs/reports/p8-final')
    frozen=verify_freeze('p8-routing-freeze-20261030',Path('evals/p8/freeze.json'))
    digest=hashlib.sha256((root/'final-dataset.json').read_bytes()).hexdigest()
    ledger=json.loads((Path('.local/p8-final-evaluations')/(digest+'.json')).read_text())
    assert ledger['stage']=='completed'
    assert ledger['report_sha256']==hashlib.sha256((public/'metrics.json').read_bytes()).hexdigest()
    rows=json.loads((root/'final-scored-private.json').read_text())
    metrics=json.loads((public/'metrics.json').read_text())
    assert len(rows)==200 and metrics['freeze_commit']==frozen['commit']
    responses={}
    for path in (root/'evaluation-returns').glob('*.json'):
        response=json.loads(path.read_text());responses[response['fingerprint']]=response
    usage={key:sum(r['usage'].get(key,0) for r in responses.values()) for key in [
        'prompt_tokens','completion_tokens','total_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens']}
    cost=(usage['prompt_cache_hit_tokens']*.006+usage['prompt_cache_miss_tokens']*.3+usage['completion_tokens']*1.2)/1e6
    supplement={'stage':'offline_summary_of_single_completed_run','freeze_commit':frozen['commit'],
      'annotation_sha256':ledger['annotation_sha256'],'groups':{},'usage':usage,
      'observed_unique_successful_responses':len(responses),'peak_usage_estimate_usd':cost,
      'price_basis':'USD per million input hit .006 / miss .3 / output 1.2; upper peak estimate, invoice unverified',
      'returned_models':sorted({r['returned_model'] for r in responses.values()}),
      'called_at_range':[min(r['called_at'] for r in responses.values()),max(r['called_at'] for r in responses.values())] if responses else [],
      'final_run_network_requests':metrics['network_requests'],'retries':metrics['retries'],'rate_limits':metrics['rate_limits']}
    md=['# P8 单次最终路由评测（2026-10-30）','',
      '冻结提交b90c201/tag `p8-routing-freeze-20261030`已先推送、后选样。人工回传200行的编号/来源/原文与原盲表一致，文件仅.local、权限600。执行器只运行一次，一次性台账已completed，原始metrics.json摘要与台账相符；本报告脚本只读已完成结果，无模型调用，不重跑路由。', '',
      '最终金标仅来自负责人盲标；银标只用来分层选样，不进真值或成绩。四根类与clarify报告子类口径保持冻结；没有看到测试结果后调规则/提示/阈值。模型、混合共享同一分类响应；kappa是人与路由/模型的一致性，不是两位人类标注者。','',
      '来源分开报告，不给合并成绩。多数类为本测试来源内的描述性多数类基线（事后可见分布，不是新拟合的线上路由）。区间为名义95% Wilson，precision分母=预测该类数、recall分母=金标该类数，均给成功数/分母。无分母记NA；固定四类macroF1将未定义F1作0，另报只含有金标类别的macro。不是加权伪计数区间，不保证总体代表性。','']
    def number(value):return 'NA' if value is None else f'{value:.4f}'
    def interval(value):
        if not value['n']:return 'NA (0/0)'
        return f"[{value['low']:.4f}, {value['high']:.4f}] ({value['successes']}/{value['n']})"
    for source,name in [('ecd_redacted','ECD'),('synthetic_rewrite','合成改写')]:
        group=[r for r in rows if r['source']==source];valid=[r for r in group if r['label'] is not None]
        counts=Counter(r['label'] for r in valid);majority=max(LABELS,key=lambda key:counts[key])
        baseline=classification_metrics([r['label'] for r in valid],[majority]*len(valid))
        subtypes=Counter(r['subtype'] for r in valid if r['label']=='clarify')
        supplement['groups'][source]={'gold_counts':{label:counts[label] for label in LABELS},
          'abstention_ids':[r['id'] for r in group if r['label'] is None],
          'abstention_roles':dict(Counter(r['role'] for r in group if r['label'] is None)),
          'majority_class':majority,'majority_metrics':baseline,'clarify_subtypes':dict(subtypes),
          'disagreements':[{key:r[key] for key in ['id','label','rule','model','hybrid','model_failure']} for r in valid if any(r[v]!=r['label'] for v in ['rule','model','hybrid'])]}
        md += [f'## {name}', '',f'总{len(group)}条，有效{len(valid)}，弃权{len(group)-len(valid)}。金标：'+json.dumps(dict(counts),ensure_ascii=False)+'。',
          'clarify报告子类（不改根类）：'+json.dumps(dict(subtypes),ensure_ascii=False)+'。',
          f'多数类 {majority}：一致率 {baseline["accuracy"]:.4f}，固定四类macroF1 {baseline["macro_f1_fixed_four"]:.4f}。', '',
          '|版本|一致率|固定四类macroF1|有金标类macroF1|kappa|预测失败|','|---|---:|---:|---:|---:|---:|']
        for version,m in metrics['groups'][source]['versions'].items():
            md.append(f'|{version}|{number(m["accuracy"])}|{number(m["macro_f1_fixed_four"])}|{number(m["macro_f1_present_gold"])}|{number(m["cohen_kappa"])}|{m["prediction_failures"]}|')
        for version,m in metrics['groups'][source]['versions'].items():
            md += ['',f'### {version} 逐类与矩阵','',
              '|类别|金标n|预测n|P|P Wilson95% (TP/预测n)|R|R Wilson95% (TP/金标n)|F1|',
              '|---|---:|---:|---:|---|---:|---|---:|']
            for label,p in m['per_class'].items():
                md.append(f'|{label}|{p["support"]}|{p["predicted"]}|{number(p["precision"])}|{interval(p["precision_wilson"])}|{number(p["recall"])}|{interval(p["recall_wilson"])}|{number(p["f1"])}|')
            md += ['', '混淆矩阵行=金标，列=预测，顺序 '+', '.join(m['prediction_label_order'])+'：','',
              '```json',json.dumps(m['confusion_matrix_rows_gold_cols_prediction']),'```']
    md += ['', '## 调用、偏差与限制','',
      f'模型{metrics["model"]}，温度0，调用日期/返回别名范围见summary.json。{metrics["network_requests"]}次网络请求，重试{metrics["retries"]}、限流{metrics["rate_limits"]}；已观察输入{usage["prompt_tokens"]}/输出{usage["completion_tokens"]}/总{usage["total_tokens"]} token，按当日官方高峰usage估价{cost:.9f}美元，账单未核验。',
      '价格依据 [DeepSeek官方价目](https://api-docs.deepseek.com/quick_start/pricing/)。先前250281 token是选样/生成准备成本，本次分类成本单列，不混同开发2000条或历史失败恢复费用。',
      '本次外发仅198条有效顾客样本的冻结分类提示，ECD129条脱敏片段与合成69条；2条明确弃权不调用模型、不进入成绩但保留计数/编号。ECD模式扫描/占位替换沿用冻结准备阶段，出站闸门再次复扫；未新增原始ECD片段。ECD论文声明脱敏匿名化，负责人批准个人项目发送官方DeepSeek，未确认公开再分发许可；原文/人工备注/用户xlsx/请求返回只.local。公开仅统计、合成全文和ECD编号→标签。',
      'ECD gold human仅2、return_request14、order_status26，未满足各类约30条；合成没有order_status金标。每层银标50不等于人工各类50，不能用银标替补金标、追加样本重评或隐去弃权。Wilson区间较宽/零分母照实，不能外推稀有类可靠性。',
      '权重只对应600候选池，未推断完整ECD或真实顾客总体成绩；合成由同源模型生成并经银标筛选，风格与选择偏差存在，须和ECD分报。没有人类双标，没有模型跨版本稳定性或重复评测。容量/限频压力、实际账单、全面隐私复核、P10/S3未验证或未做。',
      '结果解释：ECD多数类67.44%，规则72.87%/模型65.89%/混合63.57%；模型固定四类macroF1 .6038高于规则.4922，但一致率低于多数类，混合macroF1 .5898低于模型，不声称混合优于模型或全面提升。ECD模型将87条clarify中的41条判为业务动作（29查单/6退货/6人工），clarify召回46/87。human召回2/2的Wilson下界仅.3424，不能称稀有类可靠。合成模型94.20%一致率但clarify为0/3命中，缺order_status，固定四类macroF1仅.4815（有金标类.6420），不以高整体一致率掩盖缺类和零召回。',
      '完整pytest265/针对性25/ruff/strict mypy38为冻结前验证；本次离线保护与指标测试19通过、ruff报告脚本通过，只新增离线呈现和文档，未改冻结策略。没有针对最终错例修策略或进入P9。','']
    (public/'summary.json').write_text(json.dumps(supplement,ensure_ascii=False,indent=2))
    (public/'README.md').write_text('\n'.join(md))
    print(json.dumps({'offline_final_report_written':True,'rows':len(rows),'valid':sum(r['label'] is not None for r in rows),'usage':usage,'peak_estimate_usd':cost}))


if __name__=='__main__':main()
