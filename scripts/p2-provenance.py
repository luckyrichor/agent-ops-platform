"""Per-file provenance alongside every generated CSV, including non-proxy tables."""
import argparse,hashlib,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--data',type=Path,required=True);a=p.parse_args()
m=json.loads((a.data/'manifest.json').read_text())
for table,r in m['files'].items():
    metadata={'file':table+'.csv','seed':m['seed'],'synthetic':True,'rows':r['rows'],'sha256':r['sha256'],
        'intent_proxy_note':'规则代理，非人工真实意图分布，首片段说话人未确认',
        'intent_proxy_applied':table in {'tickets','ticket_events'},
        'waiting_human_ratio_independent_of_route_weights':m['waiting_human_ratio'],
        'routing_accuracy_or_evaluation_use':False,'userbehavior_fit_complete':m['user_activity_fit_complete']}
    path=a.data/(table+'.provenance.json')
    path.write_text(json.dumps(metadata,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
    r['provenance_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
(a.data/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
