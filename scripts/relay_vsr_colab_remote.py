#!/usr/bin/env python3
"""RelayVSR evaluation worker. Imports pinned upstream models without patching them."""
import hashlib
import itertools
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import time
import zipfile


def write(path, value):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value))
    tmp.replace(path)


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(4*1024*1024), b''): h.update(chunk)
    return h.hexdigest()


def command(args, **kw):
    subprocess.run([str(a) for a in args], check=True, **kw)


def bootstrap(root, spec):
    write(root/'status.json', {'state':'installing'})
    cache=Path(spec.get('cache_root',str(root)))
    repo=cache/'RelayVSR'
    if not repo.exists():
        command([sys.executable,'-m','pip','install','--disable-pip-version-check','uv==0.9.9'])
        command(['git','clone','--quiet','https://github.com/kopperx/RelayVSR.git',repo])
        command(['git','checkout','--quiet',spec['relay_commit']],cwd=repo)
        command(['uv','sync','--locked','--extra','cu128','--python','3.11'],cwd=repo)
    if subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()!=spec['relay_commit']:
        raise ValueError('relay_cache_revision_changed')
    rife=cache/'Practical-RIFE'
    if not rife.exists():
        command(['git','clone','--quiet','https://github.com/hzwer/Practical-RIFE.git',rife])
        command(['git','checkout','--quiet',spec['rife_commit']],cwd=rife)
    if subprocess.check_output(['git','rev-parse','HEAD'],cwd=rife,text=True).strip()!=spec['rife_commit']:
        raise ValueError('rife_cache_revision_changed')
    for name in ('RelayVSR','Practical-RIFE'):
        if root!=cache and not (root/name).exists():(root/name).symlink_to(cache/name,target_is_directory=True)
    with zipfile.ZipFile(root/'rife.zip') as archive:
        for item in archive.infolist():
            if item.filename.startswith('train_log/') and item.filename.endswith(('.py','.pkl')):
                archive.extract(item,rife)
    command([repo/'.venv/bin/python',__file__,'worker',root])


class Writer:
    def __init__(self, path, fps, threads=8, color_bt709=False, srgb=False):
        import av
        self.av=av; self.path=Path(path); self.count=0
        self.container=av.open(str(path),'w'); self.stream=None; self.fps=fps; self.threads=threads
        self.color_bt709=color_bt709; self.srgb=srgb
    def frame(self, rgb):
        if self.stream is None:
            self.stream=self.container.add_stream('libx264',rate=self.fps)
            self.stream.width=rgb.shape[1]; self.stream.height=rgb.shape[0]
            self.stream.pix_fmt='yuv420p'
            self.stream.options={'crf':'16','preset':'fast'}
            self.stream.codec_context.thread_count=self.threads
            if self.color_bt709 or self.srgb:
                for key in ('colorspace','color_primaries','color_trc','color_range'):
                    setattr(self.stream.codec_context,key,1)
                if self.srgb:
                    self.stream.codec_context.color_trc=13
        frame = self.av.VideoFrame.from_ndarray(rgb, format='rgb24')
        if self.srgb:
            frame = frame.reformat(format='yuv420p', dst_colorspace='ITU709')
        for p in self.stream.encode(frame):
            self.container.mux(p)
        self.count+=1
    def close(self):
        if self.stream:
            for p in self.stream.encode(): self.container.mux(p)
        self.container.close()


def worker(root, spec):
    import gc
    import numpy as np
    import torch
    import torch.nn.functional as F
    sys.path.insert(0,str(root/'RelayVSR'))
    from refvsr_inference.checkpoints import load_checkpoints
    from refvsr_inference.media import iter_frames
    from refvsr_inference.models.wan.sampling import WanVSRPipeline
    from refvsr_inference.runtime.streaming import CollaborativeFrameStream, InputFrame
    torch.set_num_threads(8); torch.set_grad_enabled(False)
    source_fps=spec.get('source_fps',24)
    target_fps=spec.get('target_fps',60)
    width,height=spec.get('output_width',2560),spec.get('output_height',1440)
    factory = spec.get('finishing_protocol') == 'factory-v1'
    geometry_ok = (16 <= min(width,height) <= 3072 and max(width,height) <= 5120) if factory else (
        16<=width<=3840 and 16<=height<=2160)
    if source_fps not in ((24,25,30,50,60) if factory else (24,30)) or width%2 or height%2 or not geometry_ok:
        raise ValueError('unsupported_finishing_geometry_or_fps')
    if target_fps not in (24,25,30,50,60) or target_fps < source_fps:
        raise ValueError('unsupported_finishing_target_fps')
    if spec['frames']*target_fps%source_fps:raise ValueError('nonintegral_output_frame_count')
    master_name=spec.get('master_filename','relay-1440p24.mp4')
    if Path(master_name).name!=master_name or not master_name.endswith('.mp4'):raise ValueError('invalid_master_name')
    # Fail on encoder/color API incompatibility before downloading or running SR.
    encoder_check=Writer(root/'encoder-preflight.mp4',source_fps,color_bt709=spec.get('color_bt709',False))
    encoder_check.frame(np.zeros((16,16,3),dtype=np.uint8));encoder_check.close()
    (root/'encoder-preflight.mp4').unlink()
    environment={'torch':torch.__version__,'gpu':torch.cuda.get_device_name(),
                 'gpu_memory_bytes':torch.cuda.get_device_properties(0).total_memory,
                 'relay_commit':spec['relay_commit'],'model_revision':spec['model_revision'],
                 'rife_commit':spec['rife_commit'],'rife_zip_sha256':sha(root/'rife.zip')}
    write(root/'environment.json',environment)
    # Validate the downstream CUDA path before spending time on full-video SR.
    sys.path.insert(0,str(root/'Practical-RIFE'))
    from train_log.IFNet_HDv3 import IFNet
    model=IFNet().cuda().eval()
    params=torch.load(root/'Practical-RIFE/train_log/flownet.pkl',map_location='cpu',weights_only=True)
    incompatible=model.load_state_dict({k.removeprefix('module.'):v for k,v in params.items()},strict=False)
    if incompatible.missing_keys or any(not k.startswith(('teacher.','caltime.')) for k in incompatible.unexpected_keys):
        raise ValueError('rife_inference_weights_mismatch')
    with torch.autocast('cuda',dtype=torch.float16):
        for t in (.2,.4,.6,.8):
            sample=model(torch.zeros(1,6,(height+255)//256*256,(width+255)//256*256,device='cuda'),t,[32,16,8,4,2])[2][-1]
            if not torch.isfinite(sample).all():raise ValueError('rife_cuda_preflight_failed')
    del params,sample
    if spec.get('resume_relay_sha256'):
        if sha(root/master_name)!=spec['resume_relay_sha256']:raise ValueError('recovered_relay_sha_mismatch')
        relay_seconds=0.0
    else:
        write(root/'status.json',{'state':'downloading_weights'})
        # Resolve only two fixed public files; verify publisher LFS digests before loading.
        weights=root/'RelayVSR/weights'; weights.mkdir(exist_ok=True)
        for name,expected in spec['weights'].items():
            dest=weights/name
            if not dest.exists():
                command(['curl','-fL','--retry','3','--silent','--show-error',
                         f'https://huggingface.co/kopper/RelayVSR/resolve/{spec["model_revision"]}/{name}', '-o',dest])
            if sha(dest)!=expected: raise ValueError('weight_integrity_failed')
        write(root/'status.json',{'state':'loading_relay'})
        wan,flash=load_checkpoints(weights/'wan.safetensors',weights/'flash.safetensors',device='cuda',dtype=torch.bfloat16)
        sampler=WanVSRPipeline(scheduler_config={'num_train_timesteps':1000,'shift':5.0},sampling={
            'execution_mode':'streaming','num_inference_steps':1,'timesteps':[1000],
            'guidance_scale':1.0,'seed':42,'kv_cache_window_size':wan.temporal_context_frames})
        sampler.prepare_streaming(model=wan,prompt_embeds=wan.prompt_embeds,device='cuda',compute_dtype=torch.bfloat16)
        bounds=[0,*spec['cuts'],spec['frames']]
        frames=iter_frames(root/'input.mp4',max_frames=spec['frames'],start_frame=spec.get('start_frame',0))
        writer=Writer(root/master_name,source_fps,color_bt709=spec.get('color_bt709',False))
        started=time.time()
        try:
            for scene,(a,b) in enumerate(zip(bounds,bounds[1:]),1):
                if time.time()>spec['deadline']-180: raise TimeoutError('resource_deadline')
                stream=CollaborativeFrameStream(wan_model=wan,flash_model=flash,wan_pipeline=sampler,
                    prompt_embeds=wan.prompt_embeds,device='cuda',compute_dtype=torch.bfloat16,window_size=16,seed=42)
                packets=(InputFrame(i,rgb,source_time=i/source_fps) for i,rgb in enumerate(itertools.islice(frames,b-a)))
                for result in stream.frames(packets):
                    if time.time()>spec['deadline']-180: raise TimeoutError('resource_deadline')
                    small=F.interpolate(result.rgb.float(),size=(height,width),mode='bicubic',align_corners=False,antialias=True)
                    rgb=small[0].add(1).mul_(127.5).clamp_(0,255).byte().permute(1,2,0).contiguous().cpu().numpy()
                    writer.frame(rgb)
                    if writer.count%24==0:
                        write(root/'status.json',{'state':'relay','scene':scene,'scenes':len(bounds)-1,
                            'frames':writer.count,'total':spec['frames'],'seconds':time.time()-started,
                            'peak_gpu_bytes':torch.cuda.max_memory_allocated()})
                del stream,result,small,rgb
                gc.collect();torch.cuda.empty_cache()
        finally:
            frames.close();writer.close()
        if writer.count!=spec['frames']:raise ValueError('relay_frame_count')
        relay_seconds=time.time()-started
        write(root/'relay-receipt.json',{'file':master_name,
            'sha256':sha(root/master_name),'bytes':(root/master_name).stat().st_size,
            'frames':writer.count,'seconds':relay_seconds})
        del wan,flash,sampler
        gc.collect();torch.cuda.empty_cache()
    write(root/'status.json',{'state':'loading_rife'})
    frames=iter_frames(root/master_name)
    previous=next(frames); following=next(frames,None); pair=0
    writer=Writer(root/'relay-rife-1440p60-silent.mp4',target_fps,
                  color_bt709=spec.get('color_bt709',False), srgb=spec.get('rife_master_srgb',False))
    def tensor(f):
        x=f[None].cuda().float()/255
        return F.pad(x,(0,(-x.shape[-1])%256,0,(-x.shape[-2])%256),mode='replicate')
    x=tensor(previous);y=tensor(following) if following is not None else x
    cutset=set(spec['cuts']); total=spec['frames']*target_fps//source_fps
    interpolation_started=time.time()
    try:
        for j in range(total):
            if time.time()>spec['deadline']-120:raise TimeoutError('resource_deadline')
            i,part=divmod(j*source_fps,target_fps)
            while pair<i:
                previous=following;following=next(frames,None);pair+=1;x=y
                y=tensor(following) if following is not None else x
            if part==0 or following is None or i+1 in cutset:
                rgb=previous.permute(1,2,0).numpy()
            else:
                with torch.autocast('cuda',dtype=torch.float16):
                    out=model(torch.cat((x,y),1),part/target_fps,[32,16,8,4,2])[2][-1]
                rgb=out[0,:,:height,:width].clamp(0,1).mul(255).round().byte().permute(1,2,0).contiguous().cpu().numpy()
            writer.frame(rgb)
            if j%60==0:write(root/'status.json',{'state':'rife','frames':j+1,'total':total,
                'seconds':time.time()-interpolation_started})
    finally:
        frames.close();writer.close()
    seek=['-ss',str(spec['start_frame']/source_fps)] if spec.get('start_frame',0) else []
    duration=[] if spec.get('preserve_full_audio') else ['-t',str(spec['frames']/source_fps)]
    command(['ffmpeg','-hide_banner','-nostdin','-n','-i',root/'relay-rife-1440p60-silent.mp4',
        *seek,'-i',root/'input.mp4','-map','0:v:0','-map','1:a:0?',
        *duration,'-c','copy','-movflags','+faststart',root/'final.mp4'])
    artifacts={}
    for name in ('final.mp4',master_name):
        command(['ffmpeg','-hide_banner','-v','error','-nostdin','-i',root/name,'-f','null','-'])
        artifacts[name]={'sha256':sha(root/name),'bytes':(root/name).stat().st_size}
    write(root/'receipt.json',{'state':'succeeded','files':artifacts,'relay_seconds':relay_seconds,
        'rife_seconds':time.time()-interpolation_started,'source_frames':spec['frames'],'source_fps':source_fps,
        'target_fps':target_fps,'output_frames':total,'frames60':total if target_fps==60 else None,
        'peak_gpu_bytes':torch.cuda.max_memory_allocated(),'environment':environment})
    write(root/'status.json',{'state':'succeeded'})


if __name__=='__main__':
    def stop_worker(signum, frame):raise TimeoutError('resource_guard_stop_preserve_outputs')
    signal.signal(signal.SIGTERM,stop_worker)
    root=Path(sys.argv[2]);spec=json.loads((root/'runtime.json').read_text())
    try:
        if sha(root/'input.mp4')!=spec['input_sha256'] or sha(root/'rife.zip')!=spec['rife_zip_sha256']:
            raise ValueError('input_integrity_failed')
        (bootstrap if sys.argv[1]=='bootstrap' else worker)(root,spec)
    except Exception as exc:
        write(root/'status.json',{'state':'failed','error':type(exc).__name__,'detail':str(exc)[:500]})
        raise
