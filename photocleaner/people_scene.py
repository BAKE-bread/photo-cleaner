"""Cheap whole-frame exclusions, independent of the semantic portrait prompt."""
from PIL import Image,ImageOps

SCENES={
 'human':[
  'a photographic portrait of a real person',
  'a photograph of a person posing for the camera',
  'a close photograph of human legs or feet wearing stockings',
  'a photograph of a person wearing an anime kigurumi mask and costume',
  'a photo of animegao kigurumi cosplay',
  'a photograph of a singer or actor performing on stage',
  'a portrait photograph printed in a photobook or magazine',
 ],
 'ritual':[
  'a documentary photograph of a traditional fire festival or a torch procession',
  'a photograph of participants at a shrine fire ritual or religious ceremony',
 ],
 'scenery':[
  'a landscape photograph of mountains with distant hikers',
  'a photograph of nature or a tourist attraction, people are incidental',
  'a photograph of a beach or sunset with distant tourists',
 ],
 'product':[
  'a screenshot of a product listing on an online shopping app',
  'a photograph of a wristwatch on an arm, the watch is the subject',
  'a photograph of merchandise or food held in a hand',
 ],
 'illustration':[
  'a drawing, anime illustration, manga or painting',
  'a virtual animated character on a screen, a hologram or a game rendering',
 ],
 'environment':[
  'a photograph documenting a concert venue, audience seats or exhibition space',
  'a photograph of a city street with tourists and strangers',
 ],
}

class SceneGuard:
    def __init__(self,config,emit):
        from dataclasses import replace
        from .vision import get_clip_core
        self.base=get_clip_core(config,emit)
        torch=self.base.torch
        text=self.base.processor(text=[p for g in SCENES.values() for p in g],padding=True,return_tensors='pt').to(self.base.device)
        from .vision import gpu_work
        with self.base.inference_lock,gpu_work(self.base.device),torch.inference_mode():
            self.features=self.base.model.get_text_features(**text);self.features/=self.features.norm(dim=-1,keepdim=True)

    def classify(self,images):
        torch=self.base.torch
        padded=[ImageOps.pad(im,(224,224),method=Image.Resampling.LANCZOS,color=(127,127,127)) for im in images]
        try:
            inputs=self.base.processor(images=padded,return_tensors='pt').to(self.base.device)
            from .vision import gpu_work
            with self.base.inference_lock,gpu_work(self.base.device),torch.inference_mode():
                features=self.base.model.get_image_features(**inputs);features/=features.norm(dim=-1,keepdim=True)
                logits=(features@self.features.T)*self.base.scale
                groups=[];offset=0
                for prompts in SCENES.values():
                    groups.append(logits[:,offset:offset+len(prompts)].max(dim=1).values);offset+=len(prompts)
                groups=torch.stack(groups,1)
                neg,which=groups[:,1:].max(1)
                # Use this guard only for a very strong negative margin. It
                # does not get to veto dolls/masks or decide human presence.
                values=torch.stack(((neg-groups[:,0]).sigmoid(),which.float()),1).cpu().tolist()
            keys=list(SCENES)[1:]
            return [{'score':row[0],'category':keys[int(row[1])],'veto':row[0]>=.98} for row in values]
        finally:
            for im in padded:im.close()
