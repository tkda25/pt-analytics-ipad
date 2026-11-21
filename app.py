import os,re,subprocess,tempfile,shutil,secrets,base64
from pathlib import Path
from fastapi import FastAPI,Request,UploadFile,File,Form,HTTPException
from fastapi.responses import HTMLResponse,FileResponse,JSONResponse
from starlette.background import BackgroundTask

app=FastAPI(title="ShortCut AI")
BASE=Path(__file__).resolve().parent
PASSWORD=os.environ.get("APP_PASSWORD","")
def ff(args):
 p=subprocess.run(["ffmpeg","-hide_banner","-nostdin","-y",*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=210)
 if p.returncode: raise RuntimeError(p.stderr[-1200:])
 return p.stderr
def probe(f):
 p=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(f)],capture_output=True,text=True,timeout=20)
 try: return float(p.stdout.strip())
 except: raise HTTPException(400,"動画を読み取れませんでした")
@app.middleware("http")
async def auth(req:Request,call_next):
 if PASSWORD:
  h=req.headers.get("authorization","")
  try:
   scheme,cred=h.split(" ",1)
   raw=base64.b64decode(cred,validate=True).decode()
   entered=raw.split(":",1)[1]
   good=scheme.lower()=="basic" and secrets.compare_digest(entered,PASSWORD)
  except: good=False
  if not good:return JSONResponse({"detail":"認証が必要です"},status_code=401,headers={"WWW-Authenticate":'Basic realm="ShortCut AI"'})
 return await call_next(req)
@app.get("/",response_class=HTMLResponse)
def home():return (BASE/"static"/"index.html").read_text(encoding="utf-8")
@app.get("/api/status")
def status():return {"ready":True,"max_upload_mb":60,"max_seconds":90,"auto_caption":bool(os.environ.get("OPENAI_API_KEY"))}
def intervals(file,duration,threshold):
 p=subprocess.run(["ffmpeg","-hide_banner","-nostdin","-i",str(file),"-af",f"silencedetect=noise=-35dB:d={threshold}","-f","null","-"],capture_output=True,text=True,timeout=90)
 if "Stream #0" not in p.stderr: raise HTTPException(400,"動画を解析できません")
 pattern=re.compile(r"silence_(start|end):\s*([0-9.]+)")
 spots=[];s=None
 for kind,n in pattern.findall(p.stderr):
  n=float(n)
  if kind=="start":s=n
  elif s is not None:spots.append((s,n));s=None
 if s is not None:spots.append((s,duration))
 keep=[];at=0
 for a,b in spots:
  if a-at>0.20:keep.append((at,min(a+0.10,duration)))
  at=max(at,b-0.08)
 if duration-at>0.20:keep.append((at,duration))
 return keep or [(0,duration)]
@app.post("/api/process")
async def process(file:UploadFile=File(...),remove_silence:bool=Form(True),caption:str=Form(""),threshold:float=Form(0.6)):
 if not (file.filename or "").lower().endswith((".mp4",".mov",".m4v",".webm")):raise HTTPException(400,"MP4/MOV/WEBMのみ対応")
 folder=Path(tempfile.mkdtemp(prefix="shortai_"))
 source=folder/"source.mp4";dest=folder/"output.mp4"
 try:
  size=0
  with source.open("wb") as out:
   while part:=await file.read(1024*1024):
    size+=len(part)
    if size>60*1024*1024:raise HTTPException(413,"60MB以下の動画を選んでください")
    out.write(part)
  duration=probe(source)
  if duration<=0 or duration>90:raise HTTPException(400,"90秒以内の動画を選んでください")
  cuts=intervals(source,duration,max(0.3,min(2.0,threshold))) if remove_silence else [(0,duration)]
  segments=[]
  for i,(a,b) in enumerate(cuts):
   seg=folder/f"seg{i}.mp4"
   ff(["-ss",f"{a:.3f}","-i",str(source),"-t",f"{b-a:.3f}","-vf","scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2","-r","30","-c:v","libx264","-preset","ultrafast","-crf","28","-threads","1","-c:a","aac","-ar","44100","-ac","2","-movflags","+faststart",str(seg)])
   segments.append(seg)
  lst=folder/"concat.txt";lst.write_text("".join("file '"+str(s)+"'\n" for s in segments))
  joined=folder/"joined.mp4"
  ff(["-f","concat","-safe","0","-i",str(lst),"-c","copy",str(joined)])
  caption=caption.strip()[:160]
  if caption:
   textfile=folder/"caption.txt";textfile.write_text(caption,encoding="utf-8")
   filter=f"drawtext=fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc:textfile={textfile}:fontcolor=white:fontsize=38:borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-235"
   ff(["-i",str(joined),"-vf",filter,"-c:v","libx264","-preset","ultrafast","-crf","28","-threads","1","-c:a","copy","-movflags","+faststart",str(dest)])
  else: shutil.move(joined,dest)
  return FileResponse(str(dest),media_type="video/mp4",filename="shortcut-ai-edited.mp4",background=BackgroundTask(shutil.rmtree,folder,ignore_errors=True))
 except HTTPException:
  shutil.rmtree(folder,ignore_errors=True);raise
 except Exception as e:
  shutil.rmtree(folder,ignore_errors=True)
  raise HTTPException(500,"動画の処理に失敗しました: "+str(e)[-300:])
