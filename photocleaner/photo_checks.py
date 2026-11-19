"""Orthogonal photographic-purpose checks, using the existing frozen Qwen model."""
import math
import numpy as np
CAPTION_RULES='''Look for text ADDED DIGITALLY to the photograph, not text printed on real objects. A: No added text, small watermark/decorative greeting, or an ordinary program's small native subtitle; a face/body censor sticker, mosaic or privacy cover is also A. B: Large post-added reaction/joke/quote captions or a designed layout with explanatory text and photographic panels. The caption/message/layout, rather than the original photograph, is the point. Output ONLY A or B.'''
PRESENTATION_RULES='''Classify the capture medium of the WHOLE image, not the subject printed inside it. A: A camera image of physical things. This includes photographed handwriting, homework/exam sheets, notebooks, book pages, paper notices, printed pictures, physical displays, merchandise or toys. Physical paper can fill the frame; use lighting, shadows, texture, curvature and perspective as evidence. A real person with a face/body privacy sticker or mosaic, or a photo with a thin border, is also A. B: A digitally created editorial/ad/catalogue layout, a direct flat scan, a software/app/web/chat screenshot, a digital table/document/QR graphic, or pure artwork/rendering. A photograph OF writing on physical paper is A, not a digital document. Output ONLY A or B.'''
LOCAL_RULES='''Ignore a local sticker, drawn face, censor blur or mosaic that covers a person's face/private body area. A: The remaining main image is a real photographed scene/person, including real surroundings, clothing, skin or real printed objects. B: The whole main image is an illustration, rendering, interface, page layout or diagram rather than a camera scene. Output ONLY A or B.'''
MINOR_RULES='''Classify the DIGITAL modifications visible in this image. A: Only privacy censoring (a sticker, drawn face, mosaic or blur covering a real face/body), thin decorative border, brief birthday greeting, or ordinary entertainment-show native frame/subtitles. The real photographed scene remains the point. B: An intentional reaction/joke/quote meme, large caption message, explanatory/ad/editorial layout, software screenshot or QR code. C: No such modifications. A privacy sticker alone is not a reaction joke; substantial multi-line quotes are B. Output ONLY A, B or C.'''
PRINTED_RULES='''Are the image's words/drawings part of physical photographed objects? A: Printed/drawn on physical merchandise, books, walls, clothing or a real screen, with perspective/surface/light and real surroundings. B: Added digitally over a photo, a meme/message/flat layout/screenshot/scan, or no meaningful physical printed object. Do not mistake digital text laid over someone's shirt for printing on fabric. Output ONLY A or B.'''
NATIVE_RULES='''A: An ordinary entertainment/variety-show frame with its original program graphics or conversational subtitles; or a personal portrait with brief decorative birthday/greeting lettering. The purpose is showing the real people/scene. B: A reaction/joke/quote image made from reused photo/film/TV, substantial multi-line caption message, editorial/ad/instruction layout or software screenshot. Distinguish native show graphics/decorative birthday greetings from a separate joke/message. Output ONLY A or B.'''
NAVIGATION_RULES='''Is this a screenshot with visible SOFTWARE or PHONE UI? A: An app/web/window/status bar, Android navigation icons or software panels belong to the digital screenshot surrounding its content. B: No such screenshot interface; a real photograph of a monitor in physical surroundings is B. Plain photo borders, caption text or subtitles alone are B. Output ONLY A or B.'''
PAPER_RULES='''Is this a CAMERA PHOTOGRAPH OF PHYSICAL PAPER? A: A real notebook, homework/exam sheet, handwritten calculation, book page or paper notice photographed in the physical world. Look for paper texture, uneven illumination, shadows, page curvature, binding, perspective, a hand or a desk; the page may fill the frame. Writing physically on paper is not a digitally added caption. B: A software/web/chat screenshot, digitally typeset document/table/diagram, direct flat scan, digital meme caption, or no photographed paper. A clean white background with digital text alone is B. Output ONLY A or B.'''
RULES='\n'.join((CAPTION_RULES,PRESENTATION_RULES,LOCAL_RULES,MINOR_RULES,PRINTED_RULES,NATIVE_RULES,NAVIGATION_RULES,PAPER_RULES,'photo-orthogonal-v4-medium-consistency'))

def navigation_candidate(image):
    if max(image.size)/min(image.size)>=1.8:return True
    a=np.asarray(image.convert('L'))
    h=max(1,round(a.shape[0]*.04))
    return any(float(b.std())<24 and (float(b.mean())<24 or float(b.mean())>231) for b in (a[:h],a[-h:]))

def combine(checks):
    cap=checks['caption'];scene=checks['presentation'];minor=checks.get('minor',0);local=checks.get('local',0);printed=checks.get('printed',0);native=checks.get('native',0);nav=checks.get('navigation',0)
    basis='画面用途与拍摄呈现'
    # A weak local positive must not overturn a strong whole-file negative.
    # Ambiguous presentations can still benefit from privacy/print review.
    presentation_ambiguous=scene>=.10
    if checks.get('paper',0)>=.98:
        cap=max(cap,checks['paper']);scene=max(scene,checks['paper']);basis='实体纸张拍摄复核'
    if printed>=.65:
        cap=max(cap,printed);basis='实体印刷/屏幕拍摄复核'
        if printed>=.9 or presentation_ambiguous:scene=max(scene,printed)
    if minor>=.50 and local>=.90:
        cap=max(cap,minor);basis='局部遮挡/装饰复核'
        if presentation_ambiguous:scene=max(scene,minor)
    if native>=.65 and local>=.90:
        cap=max(cap,native);basis='节目原生框/装饰文字复核'
        if presentation_ambiguous:scene=max(scene,native)
    rejected=cap<.5 or scene<.5 or nav>=.8
    score=math.sqrt(max(0,cap)*max(0,scene))
    return {'photo':score,'meme':1-cap,'screenshot':nav,'document':1-scene,'art':0.,'uncertain':0.,'photo_collage':0.,
        'content_rejected':rejected,'photo_checks':checks,'photo_caption_score':cap,'photo_presentation_score':scene,
        'photo_navigation_score':nav,'photo_minor_score':checks.get('minor'),'photo_local_score':checks.get('local'),
        'photo_printed_score':checks.get('printed'),'photo_native_score':checks.get('native'),'decision_route':basis,
        'photo_paper_score':checks.get('paper'),
        'photo_rejection':'界面残留' if nav>=.8 else '后期文字/反应用途' if cap<.5 else '平面排版/非拍摄呈现' if scene<.5 else ''}

class PhotoChecks:
    def __init__(self,core,batch):
        self.core=core;self.batch=batch
        self.ids=core.torch.tensor([core.tokenizer.encode(c,add_special_tokens=False)[0] for c in 'ABC'],device=core.device)
    def classify(self,images):
        checks=[{} for _ in images]
        def ask(name,rules,indices,three=False):
            if not indices:return
            values=self.core._run_labels([images[i] for i in indices],rules,self.ids[:3 if three else 2],self.batch,320)
            for i,row in zip(indices,values):checks[i][name]=row[0]
        indices=list(range(len(images)))
        ask('caption',CAPTION_RULES,indices);ask('presentation',PRESENTATION_RULES,indices)
        uncertain=[i for i,c in enumerate(checks) if min(c.values())<.5 and max(c.values())>=.30]
        ask('local',LOCAL_RULES,uncertain)
        ask('minor',MINOR_RULES,[i for i in uncertain if checks[i]['local']>=.75],True)
        ask('printed',PRINTED_RULES,uncertain)
        ask('native',NATIVE_RULES,[i for i in uncertain if checks[i]['local']>=.90 and checks[i]['caption']<.5])
        # Even a low initial document score can be a photograph of homework.
        # Existing accepted images and their other prompts are unchanged.
        ask('paper',PAPER_RULES,[i for i,c in enumerate(checks) if combine(c)['content_rejected']])
        candidates=[i for i,c in enumerate(checks) if not combine(c)['content_rejected'] and navigation_candidate(images[i])]
        ask('navigation',NAVIGATION_RULES,candidates)
        return [combine(c) for c in checks]
    def close(self):self.ids=None;self.core=None
