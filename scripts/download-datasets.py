"""Official P2 sources only. No login, cookies or credentials; raw files remain local."""
import argparse,csv,hashlib,io,json,subprocess,time,zipfile
from pathlib import Path
from datetime import datetime,UTC

SOURCES={
 'olist':('https://www.kaggle.com/api/v1/datasets/download/olistbr/brazilian-ecommerce?datasetVersionNumber=2',44717580,'CC-BY-NC-SA-4.0'),
 'ecd':('https://drive.usercontent.google.com/download?id=154J-neBo20ABtSmJDvm7DK0eTuieAuvw&export=download&confirm=t',54000633,'not specified; local personal evaluation only; do not redistribute'),
}
def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
 return h.hexdigest()

def inspect(path,name,expected):
 if path.stat().st_size!=expected:raise ValueError(name+' size differs from plan; do not substitute')
 result={'archive_bytes':path.stat().st_size,'archive_sha256':sha(path),'expected_bytes':expected,'members':[]}
 with zipfile.ZipFile(path) as z:
  bad=z.testzip()
  if bad:raise ValueError(name+' CRC failed')
  result['crc_pass']=True
  for item in z.infolist():
   if item.is_dir():continue
   h=hashlib.sha256()
   with z.open(item) as f:
    for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
   row={'name':item.filename,'uncompressed_bytes':item.file_size,'sha256':h.hexdigest()}
   with z.open(item) as f:
    if item.filename.endswith('.csv'):
     rows=csv.reader(io.TextIOWrapper(f,encoding='utf-8-sig'))
     columns=['user_id','item_id','category_id','behavior','timestamp'] if name=='userbehavior' else next(rows)
     row.update(columns=columns,rows=sum(1 for _ in rows),header_excluded=name!='userbehavior')
    elif item.filename.endswith('.txt'):
     row.update(rows=sum(1 for line in f if line.strip()),header_excluded=False)
   result['members'].append(row)
 return result

p=argparse.ArgumentParser()
p.add_argument('--raw',type=Path,default=Path('.local/datasets/raw'))
p.add_argument('--output',type=Path,default=Path('.local/datasets/verified.json'))
p.add_argument('--userbehavior-file',type=Path)
a=p.parse_args()
if '.local' not in a.raw.resolve().parts:p.error('private .local raw directory required')
a.raw.mkdir(parents=True,exist_ok=True)
report={'queried_at':datetime.now(UTC).isoformat(),'datasets':{}}
for name,(url,size,license_) in SOURCES.items():
 path=a.raw/(name+'.zip');started=time.monotonic();downloaded=False
 if not path.exists():
  command=['curl','-L','--fail','--max-time','180','--retry','1',url,'-o',str(path)]
  proc=subprocess.run(command,capture_output=True)
  if proc.returncode:raise RuntimeError(name+' download failed; retained local output, no alternate source')
  downloaded=True
 data=inspect(path,name,size)
 data.update(url=url,license=license_,path=str(path),downloaded_this_command=downloaded,
             verification_seconds=time.monotonic()-started)
 report['datasets'][name]=data
 print(json.dumps({'dataset':name,'bytes':data['archive_bytes'],'crc_pass':True}),flush=True)
if a.userbehavior_file:
 data=inspect(a.userbehavior_file,'userbehavior',949805174)
 data.update(status='available',url='https://tianchi.aliyun.com/dataset/649',license='CC-BY-NC-SA-4.0',path=str(a.userbehavior_file))
 report['datasets']['userbehavior']=data
else:
 report['datasets']['userbehavior']={'status':'requires-owner-file','downloaded_bytes':0,
   'url':'https://tianchi.aliyun.com/dataset/649','expected_archive_bytes':949805174,
   'license':'CC-BY-NC-SA-4.0 (plan metadata; file unverified)',
   'verification':'public page accessible; authenticated download not attempted; login requirement stated by owner/plan',
   'actual_rows':None,'sha256':None}
a.output.parent.mkdir(parents=True,exist_ok=True)
a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
