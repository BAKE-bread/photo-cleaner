"""Shared, short media checks and context crops; retrieval never directly passes."""
from PIL import Image
LABELS=('camera','artwork','software','meme','uncertain','photo_collage')
PRIMARY_RULES='Classify the whole image. A: A camera photograph of the real world, or a collage of photographs. B: An illustration, drawing, manga, animation, graphic design or synthetic 3D render. C: A software/app/web/chat/game interface screenshot, spreadsheet/table or text document. Answer ONLY A, B or C.'
PHYSICAL_RULES='Classify the image presentation. A: Flat original digital art, software/document screenshot, synthetic render or scanned flat page. B: A camera photo of real objects, prints or displays, with physical surfaces, lighting, edges, reflections, perspective, hands, frames or real surroundings. Output ONLY A or B.'
CAMERA_RULES='Classify the WHOLE image. A: Original artwork, software/document screenshot, text/table, synthetic render or meme. B: A real camera photograph or ordinary collage of real photographs, including physical objects, toys, prints/signs in photographed surroundings. Small watermarks allowed. Output ONLY A or B.'
PRESENTATION_RULES='Classify the presentation. A: Original digital image, scanned flat page, screenshot or synthetic render. B: A camera view of a physical scene or object, including photographed paper, walls, screens, prints and signs. Output ONLY A or B.'
from .meme import RULES as MEME_RULES
RULES=PRIMARY_RULES+'\n'+PHYSICAL_RULES+'\n'+CAMERA_RULES+'\n'+PRESENTATION_RULES+'\n'+MEME_RULES+'\nheader-v2-medium-v5'

def header_crop(image):
    # Preserve a readable top viewport for long scroll captures, whose full
    # thumbnail can make UI text disappear. All file access remains read-only.
    source=image
    key=image.info.get('source_key')
    if key and max(image.size)/min(image.size)>3:
        try:
            from pathlib import Path
            from .moving import fingerprint
            from .vision import oriented_image,rgb_image
            path=Path(key[0])
            if path.resolve()==path and fingerprint(path)==tuple(key[1:]):
                with Image.open(path) as original:
                    if original.width*original.height<=80_000_000:
                        original.load();oriented=oriented_image(original,{})
                        try:source=rgb_image(oriented)
                        finally:oriented.close()
        except (OSError,ValueError,Image.DecompressionBombError):source=image
    height=max(16,min(source.height,round(min(source.height*.33,source.width*.85))))
    result=source.crop((0,0,source.width,height));result.thumbnail((640,640),Image.Resampling.LANCZOS)
    if source is not image:source.close()
    return result

class ContentClassifier:
    def __init__(self,core,batch,cache=None):
        self.core=core;self.batch=batch;self.cache=cache
        self.ids=core.torch.tensor([core.tokenizer.encode(c,add_special_tokens=False)[0] for c in 'ABC'],device=core.device)
        from .meme import MemeClassifier
        self.memes=MemeClassifier(core,batch)
    def classify(self,images):
        values=self.core._run_labels(images,PRIMARY_RULES,self.ids,self.batch,256)
        memes=[self.cache.cached_specialist(im,'meme') if self.cache is not None else None for im in images]
        missing=[i for i,r in enumerate(memes) if r is None]
        if missing:
            for i,r in zip(missing,self.memes.classify([images[i] for i in missing])):
                memes[i]=r
                if self.cache is not None:self.cache.cached_specialist(images[i],'meme',r)
        result=[]
        for row,meme in zip(values,memes):
            result.append({'camera':row[0],'photo_collage':0.,'artwork':row[1],'software':row[2],
              'document':row[2],'meme':meme['meme_score'],'uncertain':0.,'screen_check':0.,
              'physical_check':0.,'camera_check':0.,'presentation_check':None,'photo_rescued':False,'meme_evidence':meme})
        candidates=[i for i,row in enumerate(values) if row[0]>=.35]
        if candidates:
            crops=[header_crop(images[i]) for i in candidates]
            try:
                for i,row in zip(candidates,self.core._run_labels(crops,PRIMARY_RULES,self.ids,self.batch,192)):result[i]['screen_check']=row[2]
            finally:
                for im in crops:im.close()
        art=[i for i,row in enumerate(values) if row[1]>=.35]
        if art:
            ims=[images[i] for i in art]
            physical=self.core._run_labels(ims,PHYSICAL_RULES,self.ids[:2],self.batch,256)
            camera=self.core._run_labels(ims,CAMERA_RULES,self.ids[:2],self.batch,256)
            for i,p,c in zip(art,physical,camera):
                result[i].update(physical_check=p[1],camera_check=c[1],photo_rescued=p[1]>=.60 and c[1]>=.90)
            border=[i for i in art if .50<=result[i]['physical_check']<.60 and result[i]['camera_check']>=.90]
            if border:
                values=self.core._run_labels([images[i] for i in border],PRESENTATION_RULES,self.ids[:2],self.batch,256)
                for i,row in zip(border,values):result[i].update(presentation_check=row[1],photo_rescued=row[1]>=.60)
        return result
    def close(self):self.memes.close();self.ids=None;self.core=None

def photo_scores(raw):
    best=max(('camera','artwork','software'),key=raw.get);rescued=raw.get('photo_rescued',False)
    return {'photo':raw['camera_check'] if rescued else raw['camera'],'photo_collage':0.,
            'art':raw['artwork'],'screenshot':raw['software'],'document':raw['document'],
            'meme':raw['meme'],'uncertain':raw['uncertain'],'media_scores':raw,
            'screen_check':raw.get('screen_check',0),'physical_check':raw.get('physical_check',0),
            'camera_check':raw.get('camera_check',0),'presentation_check':raw.get('presentation_check'),'photo_rescued':rescued,
            'content_rejected':raw.get('screen_check',0)>=.90 or (best!='camera' and not rescued)}
