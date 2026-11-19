"""Small frozen CLIP corroboration; a reaction keyword alone cannot select."""
import numpy as np
from PIL import ImageOps
PROMPTS={
 'reaction':['a chat reaction sticker with an exaggerated expressive face','an emoticon sticker of a cartoon character expressing a reaction','a funny reaction image used as a meme','an internet meme with a joke caption','a crudely edited photograph with meme text and reaction graphics','a humorous image macro','a cute chibi emoji used as a chat sticker'],
 'ordinary':['a normal photograph of food and drinks','a photograph of merchandise and toys','a landscape photograph','a normal anime character illustration','a beautiful digital illustration of a character','a comic page with a story','a graphic design poster or a promotional product page','a screenshot of a website or computer interface','an ordinary social media post showing a photograph','a normal portrait of a person','a document or spreadsheet table','a photograph of a pet animal','an illustration of characters posing together']}

class MemeGuard:
    def __init__(self,config,emit):
        from .vision import get_clip_core,gpu_work
        self.base=get_clip_core(config,emit);b=self.base
        with b.inference_lock,gpu_work(b.device),b.torch.inference_mode():
            x=b.processor(text=[s for v in PROMPTS.values() for s in v],padding=True,return_tensors='pt').to(b.device)
            self.text=b.model.get_text_features(**x);self.text/=self.text.norm(dim=-1,keepdim=True)
    def classify(self,images):
        from .vision import gpu_work
        b=self.base;result=[]
        for off in range(0,len(images),32):
            padded=[ImageOps.pad(im,(224,224),color=(127,127,127)) for im in images[off:off+32]]
            try:
                with b.inference_lock,gpu_work(b.device),b.torch.inference_mode():
                    x=b.processor(images=padded,return_tensors='pt').to(b.device);f=b.model.get_image_features(**x);f/=f.norm(dim=-1,keepdim=True)
                    values=(f@self.text.T).float().cpu().numpy()
                split=len(PROMPTS['reaction']);margin=values[:,:split].max(1)-values[:,split:].max(1)
                result.extend(map(float,margin))
            finally:
                for im in padded:im.close()
        return result
    def close(self):self.text=None;self.base=None
