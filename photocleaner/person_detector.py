"""Independent physical person boxes, used as corroboration, never a sole selector."""
MODEL_ID='PekingU/rtdetr_v2_r18vd'
MODEL_REVISION='5650961749fa93567c0d46fc7f43ea4f9e914107'

class PersonDetector:
    def __init__(self,config,device,emit=lambda _:None):
        import torch
        from transformers import RTDetrImageProcessor,RTDetrV2ForObjectDetection
        self.torch=torch;self.device=device;self.emit=emit;self.batch=4 if device=='cuda' else 1
        from .model_files import model_snapshot
        snapshot=model_snapshot(MODEL_ID,MODEL_REVISION,config,emit,required=('preprocessor_config.json',))
        self.processor=RTDetrImageProcessor.from_pretrained(snapshot,local_files_only=True)
        self.model=RTDetrV2ForObjectDetection.from_pretrained(snapshot,use_safetensors=True,local_files_only=True).to(device).eval()
        # Transformers 4.57's class-level anchor LRU retains `self`, hence the
        # entire detector's CUDA weights even after the application drops it.
        # Use an instance-owned cache with an explicit lifetime instead.
        from functools import lru_cache
        backbone=self.model.model
        original=type(backbone).generate_anchors.__wrapped__
        self.anchors=lru_cache(maxsize=4)(original.__get__(backbone,type(backbone)))
        backbone.generate_anchors=self.anchors
        self.person_id=next(int(k) for k,v in self.model.config.id2label.items() if v=='person')
    def close(self):
        self.anchors.cache_clear()
        self.model.model.generate_anchors=None
        self.anchors=None;self.model=None
    def classify(self,images,batch_limit=None):
        torch=self.torch;result=[]
        with torch.inference_mode():
            n=0
            while n<len(images):
                size=min(self.batch,batch_limit or self.batch)
                inputs=self.processor(images=images[n:n+size],return_tensors='pt').to(self.device)
                try:
                    from .vision import gpu_work
                    with gpu_work(self.device):output=self.model(**inputs)
                except torch.cuda.OutOfMemoryError:
                    del inputs
                    if size==1:raise
                    self.batch=max(1,size//2);torch.cuda.empty_cache();self.emit(f'人体定位显存不足：批量减为 {self.batch}，重试未判定部分。');continue
                probabilities=output.logits.sigmoid();confidence,labels=probabilities.max(-1)
                confidence=confidence*(labels==self.person_id)
                data=torch.cat((confidence.unsqueeze(-1),output.pred_boxes),-1).cpu().tolist()
                for rows in data:
                    # Normalized cx,cy,w,h from the detector. Keep only useful
                    # boxes, bounded to image edges for area comparisons.
                    boxes=[]
                    for score,cx,cy,w,h in rows:
                        if score<.45:continue
                        x1=max(0,cx-w/2);x2=min(1,cx+w/2);y1=max(0,cy-h/2);y2=min(1,cy+h/2)
                        boxes.append({'score':score,'area':max(0,x2-x1)*max(0,y2-y1),'box':[x1,y1,x2,y2]})
                    result.append(sorted(boxes,key=lambda b:b['score'],reverse=True))
                del output,inputs,probabilities,confidence,labels
                n+=size
        return result
