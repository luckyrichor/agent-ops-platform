"""Verify two complete generations, including every CSV and provenance sidecar."""
import argparse,hashlib,json
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--first',type=Path,required=True);p.add_argument('--second',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
def hashfile(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
 return h.hexdigest()
x=json.loads((a.first/'manifest.json').read_text());y=json.loads((a.second/'manifest.json').read_text())
assert x['seed']==y['seed'] and x['source_fit_sha256']==y['source_fit_sha256']
files={}
for name,r in x['files'].items():
 other=y['files'][name]
 for key in ['rows','bytes','sha256','provenance_sha256']:assert r[key]==other[key],name
 for root,row in [(a.first,r),(a.second,other)]:
  assert hashfile(Path(row['path']))==row['sha256']
  assert hashfile(root/(name+'.provenance.json'))==row['provenance_sha256']
  meta=json.loads((root/(name+'.provenance.json')).read_text())
  assert meta['intent_proxy_note']=='规则代理，非人工真实意图分布，首片段说话人未确认'
  assert not meta['routing_accuracy_or_evaluation_use']
 files[name]={k:r[k] for k in ['rows','bytes','sha256','provenance_sha256']}
checksum=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
r={'seed':x['seed'],'full_scale_runs':2,'all_files_identical':True,'dataset_checksum':checksum,'files':files,
 'generation_seconds':[x['generation_seconds'],y['generation_seconds']],
 'intent_proxy_note':x['intent_proxy_note'],'waiting_human_ratio':x['waiting_human_ratio'],
 'routing_accuracy_or_evaluation_use':False,'userbehavior_fit_complete':x['user_activity_fit_complete']}
a.output.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'identical':True,'dataset_checksum':checksum}))
