import os,re,subprocess,tempfile,shutil,secrets,base64
from pathlib import Path
from fastapi import FastAPI,Request,UploadFile,File,Form,HTTPException
from fastapi.responses import HTMLResponse,FileResponse,JSONResponse
from starlette.background import BackgroundTask

app=FastAPI(title="ShortCut AI")
BASE=Path(__file__).resolve().parent
PASSWORD=os.environ.get("APP_PASSWORD","")
MAX_TOTAL=120*1024*1024
def run(cmd,timeout=210):
 p=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout)
 if p.returncode: raise RuntimeError(p.stderr[-1100:])
 return p
def ff(args,timeout=210):return run(["ffmpeg","-hide_banner","-nostdin","-y",*args],timeout)
def duration(file):
 try:return float(run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(file)],20).stdout.strip())
 except:raise HTTPException(400,"動画・音声を読み取れません")
def audio_exists(file):
 try:
  p=run(["ffprobe","-v","error","-select_streams","a:0","-show_entries","stream=index","-of","csv=p=0",str(file)],20)
  return bool(p.stdout.strip())
 except:return False
@app.middleware("http")
async def auth(req:Request,call_next):
 if PASSWORD:
  try:
   scheme,cred=req.headers.get("authorization","").split(" ",1)
   raw=base64.b64decode(cred,validate=True).decode()
   entered=raw.split(":",1)[1]
   valid=scheme.lower()=="basic" and secrets.compare_digest(entered,PASSWORD)
  except:valid=False
  if not valid:return JSONResponse({"detail":"ログインしてください"},status_code=401,headers={"WWW-Authenticate":'Basic realm="ShortCut AI"'})
 return await call_next(req)
@app.get("/",response_class=HTMLResponse)
def home():return (BASE/"static"/"index.html").read_text(encoding="utf-8")
@app.get("/api/status")
def status():return {"ready":True,"max_total_mb":120,"max_clips":5,"max_total_seconds":180,"bgm":True,"reference_prompt":True}
def intervals(file,length,threshold):
 p=subprocess.run(["ffmpeg","-hide_banner","-nostdin","-i",str(file),"-af",f"silencedetect=noise=-35dB:d={threshold}","-f","null","-"],capture_output=True,text=True,timeout=120)
 if "Stream #0" not in p.stderr:raise HTTPException(400,"動画を解析できません")
 spots=[];s=None
 for typ,n in re.findall(r"silence_(start|end):\s*([0-9.]+)",p.stderr):
  n=float(n)
  if typ=="start":s=n
  elif s is not None:spots.append((s,n));s=None
 if s is not None:spots.append((s,length))
 parts=[];pos=0
 for a,b in spots:
  if a-pos>0.23:parts.append((pos,min(a+0.08,length)))
  pos=max(pos,b-0.06)
 if length-pos>0.23:parts.append((pos,length))
 return parts or [(0,length)]
async def store(upload,path,used):
 with path.open("wb") as out:
  while chunk:=await upload.read(1024*1024):
   used+=len(chunk)
   if used>MAX_TOTAL:raise HTTPException(413,"全素材の合計は120MB以下にしてください")
   out.write(chunk)
 return used
@app.post("/api/process")
async def process(files:list[UploadFile]=File(...),music:UploadFile|None=File(None),remove_silence:bool=Form(True),caption:str=Form(""),music_volume:float=Form(0.25),original_volume:float=Form(1.0),style:str=Form("standard")):
 if not 1<=len(files)<=5:raise HTTPException(400,"動画は1〜5本を選択してください")
 folder=Path(tempfile.mkdtemp(prefix="shortcut_"))
 try:
  total_bytes=0;raw=[];total_time=0
  for i,item in enumerate(files):
   if not (item.filename or "").lower().endswith((".mp4",".mov",".m4v",".webm")):raise HTTPException(400,"動画はMP4/MOV/WEBMのみ対応")
   f=folder/f"input_{i}{Path(item.filename).suffix.lower()}"
   total_bytes=await store(item,f,total_bytes)
   d=duration(f);total_time+=d
   if d<=0 or d>180 or total_time>180:raise HTTPException(400,"動画の合計は180秒以内にしてください")
   raw.append((f,d))
  music_path=None
  if music and music.filename:
   if not music.filename.lower().endswith((".mp3",".m4a",".wav",".aac",".mp4")):raise HTTPException(400,"BGMはMP3/M4A/WAV/AACのみ対応")
   music_path=folder/"bgm_audio"+Path(music.filename).suffix.lower()
   total_bytes=await store(music,music_path,total_bytes)
   if not audio_exists(music_path):raise HTTPException(400,"BGMの音声を読み取れません")
  cutthreshold=0.38 if style=="fast" else (0.9 if style=="relaxed" else 0.6)
  segments=[]
  for i,(src,d) in enumerate(raw):
   spans=intervals(src,d,cutthreshold) if remove_silence and audio_exists(src) else [(0,d)]
   for j,(start,end) in enumerate(spans):
    seg=folder/f"part_{i}_{j}.mp4"
    trim=max(0.1,end-start)
    common=["-ss",f"{start:.3f}","-i",str(src)]
    if audio_exists(src):
     audio_in=[];audio_map=["-map","0:a:0"]
    else:
     audio_in=["-f","lavfi","-i","anullsrc=r=44100:cl=stereo"];audio_map=["-map","1:a:0"]
    ff([*common,*audio_in,"-t",f"{trim:.3f}","-map","0:v:0",*audio_map,"-vf","scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2","-r","30","-c:v","libx264","-preset","ultrafast","-crf","29","-threads","1","-c:a","aac","-ar","44100","-ac","2","-shortest",str(seg)])
    segments.append(seg)
  if not segments:raise HTTPException(400,"編集できる映像がありません")
  lst=folder/"list.txt";lst.write_text("".join("file '"+str(p)+"'\n" for p in segments))
  joined=folder/"joined.mp4"
  ff(["-f","concat","-safe","0","-i",str(lst),"-c","copy",str(joined)])
  vf=[]
  caption=caption.strip()[:160]
  if caption:
   capfile=folder/"caption.txt";capfile.write_text(caption,encoding="utf-8")
   vf.append(f"drawtext=fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc:textfile='{capfile}':fontcolor=white:fontsize=38:borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-235")
  dest=folder/"result.mp4"
  args=["-i",str(joined)]
  if music_path:args+=["-stream_loop","-1","-i",str(music_path)]
  if music_path:
   mv=max(0,min(1,float(music_volume)));ov=max(0,min(1,float(original_volume)))
   args+=["-filter_complex",f"[0:a]volume={ov}[voice];[1:a]volume={mv}[bgm];[voice][bgm]amix=inputs=2:duration=first:dropout_transition=0[aout]","-map","0:v:0","-map","[aout]"]
  elif original_volume!=1:
   args+=["-filter:a",f"volume={max(0,min(1,float(original_volume)))}"]
  if vf:args+=["-vf",",".join(vf)]
  args+=["-c:v","libx264","-preset","ultrafast","-crf","29","-threads","1","-c:a","aac","-movflags","+faststart",str(dest)]
  ff(args,timeout=240)
  return FileResponse(str(dest),filename="shortcut-ai-edited.mp4",media_type="video/mp4",background=BackgroundTask(shutil.rmtree,folder,ignore_errors=True))
 except HTTPException:
  shutil.rmtree(folder,ignore_errors=True);raise
 except Exception as e:
  shutil.rmtree(folder,ignore_errors=True)
  raise HTTPException(500,"編集処理エラー: "+str(e)[-260:])
