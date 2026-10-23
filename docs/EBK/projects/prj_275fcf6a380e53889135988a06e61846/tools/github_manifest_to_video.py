#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, subprocess, tempfile, shutil
from pathlib import Path
from PIL import Image, ImageOps, ImageDraw

EXTS=['.png','.jpg','.jpeg','.webp']
PORTRAIT_FOLDERS={
 'Owner':'owner','Manager':'manager','Operator':'operator','Assistant':'assistant',
 'StaffLeader':'staffleader','Staff':'staff','SwitchCast':'switchcast',
 'CastLeader':'castleader','UpperCast':'uppercast','DownerCast':'downercast',
 'ConditionalCast':'condisionalcast','CondisionalCast':'condisionalcast'
}
POSTER_FOLDERS={
 'EventPoster':'main','FrontPoster':'front','SidePoster':'side','BackPoster':'back',
 'Notice':'notice','Menu':'menu','Kanpe':'kanpe'
}

def find_image(base:Path,kind:str,category:str,slot:int):
    folder=(PORTRAIT_FOLDERS if kind=='member' else POSTER_FOLDERS).get(category,category.lower())
    root=base/('portraits' if kind=='member' else 'posters')/folder
    for ext in EXTS:
        p=root/f'slot{slot}{ext}'
        if p.is_file(): return p
    return None

def raw_url(repo:str,branch:str,path:Path):
    return f'https://raw.githubusercontent.com/{repo}/{branch}/{path.as_posix()}'

def make_frame(src:Path|None,dst:Path,w:int,h:int,stretch:bool,label:str):
    if src is None:
        im=Image.new('RGB',(w,h),(20,20,20))
        d=ImageDraw.Draw(im); d.text((40,40),'MISSING: '+label,fill=(255,80,80))
        im.save(dst); return 1, 0, 0
    with Image.open(src) as im:
        im=ImageOps.exif_transpose(im).convert('RGBA')
        source_width, source_height = im.size
        if stretch:
            out=im.resize((w,h),Image.Resampling.LANCZOS)
        else:
            out=Image.new('RGBA',(w,h),(0,0,0,0))
            cp=im.copy(); cp.thumbnail((w,h),Image.Resampling.LANCZOS)
            out.paste(cp,((w-cp.width)//2,(h-cp.height)//2))
        out.save(dst)
    return 0, source_width, source_height

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--repository',required=True,help='owner/repository')
    ap.add_argument('--branch',default='main')
    ap.add_argument('--manifest',default='data/roster_manifest.video.generated.json')
    ap.add_argument('--output',default='videos/roster.mp4')
    ap.add_argument('--frame-map-output',default='data/roster_frame_map.txt')
    ap.add_argument('--save-url-list',default='data/roster_resolved_urls.txt')
    ap.add_argument('--fps',type=int,default=30)
    ap.add_argument('--frames-per-entry',type=int,default=3)
    ap.add_argument('--sample-frame',type=int,default=1)
    ap.add_argument('--width',type=int,default=1920)
    ap.add_argument('--height',type=int,default=1080)
    ap.add_argument('--resize-mode',choices=['stretch','contain'],default='stretch')
    ap.add_argument('--poster-columns',type=int,default=5)
    ap.add_argument('--poster-rows',type=int,default=2)
    ap.add_argument('--published-manifest',default='data/roster_manifest.video.published.json')
    a=ap.parse_args()

    manifest=json.loads(Path(a.manifest).read_text(encoding='utf-8'))
    entries=[]
    for item in manifest.get('members',[]):
        if not isinstance(item,dict) or not item.get('enabled',True): continue
        if not item.get('showPortrait',True): continue
        entries.append(('member',str(item.get('category','')),int(item.get('slot',0))))
    for item in manifest.get('posters',[]):
        if not isinstance(item,dict) or not item.get('enabled',True): continue
        entries.append(('poster',str(item.get('category','')),int(item.get('slot',0))))
    from build_room_media_slides import slide_records,contact_sheets,frame_bytes,delivery_settings
    delivery=delivery_settings(Path.cwd())
    dual_matte=delivery["alphaMode"]=="dual-matte"
    encoded_width=a.width*(2 if dual_matte else 1)
    if delivery["layout"]=="single": a.poster_columns,a.poster_rows=1,1
    if not delivery["enabled"]:
        published=dict(manifest);published["videoRoster"]={"enabled":False,"status":"individual-images"}
        Path(a.published_manifest).parent.mkdir(parents=True,exist_ok=True)
        Path(a.published_manifest).write_text(json.dumps(published,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        return
    slide_images,missing_slides=slide_records(Path.cwd())
    if missing_slides: raise ValueError('Missing configured slide images: '+', '.join(missing_slides))
    slide_paths={tuple(key.split('|')[:2]+[int(key.split('|')[2])]):path for key,path in slide_images}
    entries+=list(slide_paths)

    fps=max(1,a.fps); frames=max(1,a.frames_per_entry)
    sample=max(0,min(a.sample_frame,frames-1))
    if fps>60 or frames>10: raise ValueError('FPS/frame count exceeds the supported settings')
    if not 64<=a.width<=1920 or not 64<=a.height<=2160 or a.width%2 or a.height%2:
        raise ValueError('Logical video dimensions must be even and fit the black-white video limits')
    Path(a.output).parent.mkdir(parents=True,exist_ok=True)
    Path(a.frame_map_output).parent.mkdir(parents=True,exist_ok=True)

    with tempfile.TemporaryDirectory(prefix='ebk-video-roster-') as td:
        frame_dir=Path(td)/'frames'; frame_dir.mkdir()
        lines=[]; urls=[]

        if not 1 <= a.poster_columns <= 8 or not 1 <= a.poster_rows <= 8:
            raise ValueError('poster grid must be between 1 and 8 rows/columns')
        paths=[]
        for kind,category,slot in entries:
            src=slide_paths.get((kind,category,slot)) if kind=='slide' else find_image(Path('images'),kind,category,slot)
            if src is None: raise ValueError(f'missing configured image: {kind}|{category}|{slot}')
            paths.append(src)
        sheets,geometry=contact_sheets(paths,frame_dir/'sheets',a.width,a.height,a.poster_columns,a.poster_rows)
        for i,(kind,category,slot) in enumerate(entries):
            item=geometry[i];x,y,w,h=item['uv'];page=item['sheet'];sample_index=page*frames+sample
            url=raw_url(a.repository,a.branch,paths[i]);urls.append(url)
            lines.append(f'{sample_index}|{kind}|{category}|{slot}|{url}|{w:.8f}|{h:.8f}|{x:.8f}|{y:.8f}|'
                f'page={page}|frames={frames}|sampleFrame={sample}|sample={(sample_index+.5)/fps:.8f}|bad=0|'
                f'aspect={item["aspect"]:.8f}|width={item["sourceWidth"]}|height={item["sourceHeight"]}|fit=contain|'
                f'sheetColumns={a.poster_columns}|sheetRows={a.poster_rows}|tile={item["tile"]}')
        for page,sheet_path in enumerate(sheets):
            packed=Image.frombytes('RGB',(encoded_width,a.height),frame_bytes(sheet_path,a.width,a.height,dual_matte))
            first=frame_dir/f'frame_{page*frames+1:06d}.png';packed.save(first)
            for r in range(1,frames):shutil.copyfile(first,frame_dir/f'frame_{page*frames+r+1:06d}.png')
        if not sheets:
            # Empty poster settings must not introduce a sample poster.
            empty=Image.new('RGB',(encoded_width,a.height),(0,0,0))
            if dual_matte: empty.paste((255,255,255),(a.width,0,encoded_width,a.height))
            empty.save(frame_dir/'frame_000001.png')

        Path(a.frame_map_output).write_text('\n'.join(lines)+'\n',encoding='utf-8')
        Path(a.save_url_list).write_text('\n'.join(urls)+'\n',encoding='utf-8')

        subprocess.run([
            'ffmpeg','-y','-framerate',str(fps),'-start_number','1',
            '-i',str(frame_dir/'frame_%06d.png'),
            '-r',str(fps),'-c:v','libx264','-pix_fmt','yuv420p',
            '-qp','10','-g','1','-bf','0','-sc_threshold','0',
            '-tune','psnr','-an','-movflags','+faststart',a.output
        ],check=True)
    published=dict(manifest)
    video=dict(manifest.get('videoRoster') or {})
    video.update(enabled=True,format='ebk-dual-matte-sbs-srgb-v1' if dual_matte else 'ebk-roster-v1',fps=fps,
        framesPerEntry=frames,sampleFrame=sample,frameCount=max(1,len(sheets)*frames),
        logicalWidth=a.width,logicalHeight=a.height,encodedWidth=encoded_width,encodedHeight=a.height,
        videoUrl=raw_url(a.repository,a.branch,Path(a.output)),
        frameMapUrl=raw_url(a.repository,a.branch,Path(a.frame_map_output)),
        sheetColumns=a.poster_columns,sheetRows=a.poster_rows)
    published['videoRoster']=video
    Path(a.published_manifest).parent.mkdir(parents=True,exist_ok=True)
    Path(a.published_manifest).write_text(json.dumps(published,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('built',a.output,'entries',len(entries))

if __name__=='__main__':
    main()
