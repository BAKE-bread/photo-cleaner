"""Semantic reaction-image selection; no size/blur shortcut defines a meme."""
LABELS=('reaction','macro','comic_joke','joke_screen','ordinary','uncertain')
NAMES={'reaction':'反应表情/贴纸','macro':'配字/改图表情包','comic_joke':'梗图/短段子','joke_screen':'段子/反应截图','ordinary':'普通图像','uncertain':'无法确认'}
BINARY_RULES='Classify this image. A: Ordinary photo/artwork. B: Internet meme or chat reaction sticker. Output ONLY A or B.'
CONTEXT_RULES='Classify the whole image. A: Ordinary photograph or photo of physical printed/cartoon merchandise, normal illustration, story comic or poster. B: A standalone chat reaction sticker, meme edit or short gag made to convey a reaction or joke. Output ONLY A or B.'
TYPE_RULES='For this reaction/meme image choose its main form. Output ONLY A-D.\nA: Expressive reaction sticker/emoticon.\nB: Captioned image macro, edited reaction photograph or exaggerated meme edit.\nC: Short visual joke or meme comic with a punchline.\nD: Joke screenshot or cropped chat/post interaction with a punchline.'
STICKER_RULES='A: A standalone simple expressive chat sticker/emoticon: a deliberately simplified flat/doodled face or head with large expressive eyes, isolated on plain/transparent background. B: An ordinary anime character portrait, episode screenshot, full illustration, story comic, real animal/photo/toy/product, scientific diagram or software interface. A recognizable ordinary character alone is not A. Output ONLY A or B.'
STICKER_PHYSICAL_RULES='Classify the image presentation. A: Flat original digital art, software/document screenshot, synthetic render or scanned flat page. B: A camera photo of real objects, prints or displays, with physical surfaces, lighting, edges, reflections, perspective, hands, frames or real surroundings. Output ONLY A or B.'
from .meme_guard import PROMPTS as GUARD_PROMPTS
RULES=BINARY_RULES+'\n'+CONTEXT_RULES+'\n'+TYPE_RULES+'\n'+STICKER_RULES+'\n'+STICKER_PHYSICAL_RULES+'\n'+str(GUARD_PROMPTS)+'\nmeme-v5-sticker-rescue2'

class MemeClassifier:
    def __init__(self,core,batch):
        self.core=core;self.batch=batch
        self.ids=core.torch.tensor([core.tokenizer.encode(c,add_special_tokens=False)[0] for c in 'ABCDEF'],device=core.device)
        from .meme_guard import MemeGuard
        self.guard=MemeGuard(core.config,core.emit)
    def classify(self,images):
        import math
        margins=self.guard.classify(images)
        result=[{'reaction':0.,'macro':0.,'comic_joke':0.,'joke_screen':0.,'ordinary':1.,'uncertain':0.,'meme_score':0.,'qwen_meme_score':None,'meme_context_score':None,'clip_margin':m,'clip_meme_score':1/(1+math.exp(-80*m))} for m in margins]
        candidates=[i for i,m in enumerate(margins) if m>=0]
        if candidates:
            values=self.core._run_labels([images[i] for i in candidates],BINARY_RULES,self.ids[:2],self.batch,256)
            for i,v in zip(candidates,values):
                score=math.sqrt(v[1]*result[i]['clip_meme_score']);result[i].update(meme_score=score,qwen_meme_score=v[1],reaction=score,ordinary=1-score)
        # A recognisable reaction character printed on a real product, or an
        # ordinary comic, is not sufficient. Only this shortlist needs a
        # second purpose check; rejected uncertainty cannot veto a photo.
        check=[i for i,r in enumerate(result) if r['meme_score']>=.2]
        if check:
            values=self.core._run_labels([images[i] for i in check],CONTEXT_RULES,self.ids[:2],self.batch,256)
            for i,row in zip(check,values):
                result[i]['meme_context_score']=row[1]
                if row[1]<.9:result[i].update(meme_score=0.,reaction=0.,ordinary=1.)
        rescue=[i for i,r in enumerate(result) if r['meme_score']<.8 and (r.get('qwen_meme_score') or 0)>=.5 and r['clip_margin']>=.025]
        if rescue:
            values=self.core._run_labels([images[i] for i in rescue],STICKER_RULES,self.ids[:2],self.batch,256)
            physical=[]
            for i,row in zip(rescue,values):
                result[i]['sticker_rescue_score']=row[0]
                if row[0]>=.9:physical.append(i)
            if physical:
                values=self.core._run_labels([images[i] for i in physical],STICKER_PHYSICAL_RULES,self.ids[:2],self.batch,256)
                for i,row in zip(physical,values):
                    r=result[i];r['sticker_physical_score']=row[1]
                    if row[1]<.6:
                        score=(r['sticker_rescue_score']*r['qwen_meme_score']*r['clip_meme_score'])**(1/3)
                        if score>r['meme_score']:r.update(meme_score=score,reaction=score,ordinary=1-score,decision_route='独立贴纸补判；实体拍摄排除通过')
        selected=[i for i,r in enumerate(result) if r['meme_score']>=.5]
        if selected:
            types=self.core._run_labels([images[i] for i in selected],TYPE_RULES,self.ids[:4],self.batch,192)
            for i,row in zip(selected,types):
                for key,value in zip(LABELS[:4],row):result[i][key]=result[i]['meme_score']*value
        return result
    def close(self):self.guard.close();self.ids=None;self.core=None

def decide(scores,config):
    value=scores.get('meme_score',sum(scores.get(k,0) for k in LABELS[:4]));best=max(LABELS,key=lambda k:scores.get(k,0))
    from .thresholds import lower
    return lower(value,config.meme_select_threshold,config),{
        'category_scores':scores,'meme_score':round(value,6),'meme_category':best,'meme_category_label':NAMES[best],
        'decision_route':scores.get('decision_route','CLIP + Qwen 表情包核对'),'sticker_rescue_score':scores.get('sticker_rescue_score'),'qwen_meme_score':scores.get('qwen_meme_score'),'meme_context_score':scores.get('meme_context_score'),'clip_margin':scores.get('clip_margin'),'clip_meme_score':scores.get('clip_meme_score'),**{k+'_score':scores.get(k,0) for k in LABELS}}
