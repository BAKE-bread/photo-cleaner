"""Fixed zero-shot descriptions. Samples are for evaluation, never training."""
import json

PERSON_PROMPTS={
 'portrait':[
  'a portrait photograph focused on a real person, their face or their body',
  'a selfie of a person posing for the camera indoors or outdoors',
  'a close-up photograph of a human face',
  'a photograph of a person showing their outfit in front of a mirror',
  'a photograph of a person posing, even if their face is covered or hidden',
  'a photograph of a small person posing in a landscape, the person is the subject',
  'a photograph of a person showing their legs, feet, stockings or shoes',
  'a close-up photograph of a human body, a cropped portrait without a face',
  'a photograph of a person wearing a skirt and tights, only the lower body is visible',
  'a photo of a woman wearing stockings',
  'a photo of feet wearing socks',
  'a photograph of human legs',
  'a portrait photograph',
  'a photo of legs in pantyhose',
  "a photo of a woman's legs in stockings",
  'a selfie of legs',
  'a closeup photo of feet in tights',
 ],
 'cosplay':[
  'a cosplay photograph of a real person dressed as an anime character',
  'a photograph of a cosplayer posing in a costume',
  'a photograph of a person wearing an anime kigurumi mask and costume',
  'a photograph of a real person dressed in an elaborate fantasy costume',
  'animegao kigurumi cosplay, a human wearing an anime face mask and a full body costume',
  'a kigurumi cosplayer wearing a large anime mask, a wig and a school uniform',
  'a human dressed in an anime character costume and skin suit posing for a photo',
  'a photo of animegao cosplay',
  'a photo of kigurumi cosplay',
  'a photo of a masked cosplayer',
  'a photo of someone wearing a doll mask',
  'a photograph of a woman wearing an anime doll head mask',
  'a photo of a life size anime doll costume',
  'a person wearing a doll suit',
  'an animegao kigurumi performer',
 ],
 'activity':[
  'a photograph of friends posing together at an event',
  'a photograph of fans taking a group selfie at a concert',
  'a close photograph of friends celebrating and clinking glasses together at dinner',
  'a photograph of a concert fan holding a glow stick or fan merchandise, showing their body',
 ],
 'performance':[
  'a photograph focused on a singer or musician performing on a stage',
  'a close photograph of performers dancing or acting on stage',
  'a photograph of an idol performing live, the performer is the subject',
 ],
 'depicted_person':[
  'a screenshot of a cosplay photo posted on social media, focused on the cosplayer',
  'a screenshot from a movie or television drama focused on a real actor or their face',
  'a printed portrait of a person in a fashion magazine',
  'a portrait of a human face printed on a magazine page',
  'a collage of human portrait photographs',
 ],
 'crowd_environment':[
  'a wide photograph of an entire concert venue, seats, audience and distant stage',
  'a photograph of a crowded exhibition hall, the space and crowd are the subject',
  'a photograph of a parade with floats, participants and spectators in the street',
  'a photograph of a city street with pedestrians and passersby',
  'a candid street photograph of a stranger or a passerby',
  'a photograph of people welcoming visitors during a trip or tour',
  'a photograph documenting the interior of a theater or exhibition, people are incidental',
  'a documentary photograph of a traditional fire festival, a torch procession or a religious ceremony',
  'a photograph of a public train or bus interior with passengers',
  'a tourist photograph of a park, mountain, forest or beach with distant visitors',
 ],
 'display_doll':[
  'a photograph of a ball jointed toy doll with visible articulated joints',
  'a photograph of a shop mannequin in a clothing store display window',
  'a photograph of a freestanding cardboard character cutout mounted on a display stand',
  'a photograph of a display board with printed character portraits in an exhibition',
  'a photograph of a miniature collectible anime figurine on a plastic base',
  'a photograph of a small toy doll with miniature furniture',
 ],
 'object_food':[
  'a photograph focused on food held in a hand, only the hand is visible',
  'a photograph focused on a collectible or toy held in a hand',
  'a photograph of merchandise, a collection of goods or souvenirs on a table',
  'a photograph of food, a drink, or a meal without people celebrating',
 ],
 'other':[
  'an anime illustration or a drawing of a character',
  'a photograph of a landscape, building, empty room or scenery',
  'a screenshot of a game, text, chat messages or a computer interface',
  'a graphic design advertisement or typography poster',
  'a photograph of an animal or an ordinary object',
  'a landscape photograph of mountains, cliffs or a valley',
  'a photograph of a beach, ocean or coastline',
  'a photograph of a forest, trees, a path or flowers',
  'a photograph of the sky, clouds, a sunset or fireworks',
  'a photograph of a building, a road, a vehicle or a room interior',
  'a photograph of a cat, dog, rabbit or a stuffed animal',
  'an anime screenshot, a manga comic page or a cartoon illustration',
  'a photograph of a ticket, a book cover, a souvenir or a handwritten document',
  'a photograph of a scenic landscape',
  'a photograph of nature',
  'a manga comic page',
  'a screenshot of a text post on social media',
  'a photograph of souvenirs and merchandise',
  'a photograph of a plush toy',
  'a photograph of a cat',
  'a photograph of a dog',
  'a photograph of a rabbit',
  'a screenshot of a tweet with text',
  'a screenshot of a chat conversation',
  'a photograph of a paper document',
  'a photograph of a pet wearing a costume',
  'a cat wearing a headscarf',
  'a dog wearing a headscarf',
  'a comic strip',
  'a screenshot of an anime',
 ],
}
TARGETS=('portrait','cosplay','activity','performance','depicted_person')
PERSON_LABELS={'portrait':'人像/自拍/特写','cosplay':'Cosplay/Kigurumi','activity':'人物活动/舞台表演','performance':'舞台表演',
               'depicted_person':'截图/印刷人物照片','crowd_environment':'会场/人群/街道环境','display_doll':'玩偶/立牌/展示物','object_food':'手持物品/食品/收藏品','other':'其他图画/景物/界面','uncertain':'无法确认主体，原地保留'}


def decide_person(scores,threshold,verify_threshold=.80,inclusive=True):
    target=max((scores.get(k,0) for k in TARGETS),default=0)
    negative=max((scores.get(k,0) for k in PERSON_LABELS if k not in TARGETS),default=0)
    # The classifier supplies a binary comparison as well as descriptive scores.
    value=scores.get('people',target/(target+negative) if target+negative else 0)
    best=max((k for k in scores if k in PERSON_LABELS),key=scores.get)
    semantic=scores.get('semantic_model',False)
    accepted=(value>=threshold if inclusive else value>threshold) and (not semantic or best in TARGETS) and not scores.get('verification_rejected',False)
    if semantic:accepted=accepted and (scores.get('verification_score',0)>=verify_threshold if inclusive else scores.get('verification_score',0)>verify_threshold)
    rescued=semantic and not accepted and scores.get('rescue_accepted',False)
    if rescued:accepted=True
    if scores.get('illustration_rejected',False):accepted=False;rescued=False
    code=scores.get('verification_label','')
    scene_names={'ritual':'祭典/仪式','scenery':'景物/旅游环境','product':'商品/手持物品','illustration':'绘画/虚拟画面','environment':'会场/街道环境'}
    conclusion='主体复核通过' if code=='A' else '主体复核不通过' if code=='B' else '场景排除：'+scene_names.get(code[6:],code[6:]) if code.startswith('scene:') else '初判未通过，未复核' if semantic else ''
    category_label=PERSON_LABELS.get(best,best)
    if rescued:
        typed=scores.get('rescue_scores',{})
        if typed:
            chosen=max(typed,key=typed.get)
            best='portrait' if chosen=='body' else chosen
            category_label='人体局部/腿足' if chosen=='body' else PERSON_LABELS.get(best,best)
        conclusion='常规筛选未通过；'+scores.get('rescue_basis','补判')+'入选'
    return accepted,{'people_score':round(float(value),6),'person_category':best,'person_category_label':category_label,
                            'category_scores':scores,'content_score':scores.get('content_score',value),'verification_score':scores.get('verification_score'),
                            'verification_label':conclusion,
                            'decision_basis':scores.get('rescue_basis') if rescued else '语义初判 + 主体复核' if semantic else '单图内容'}


def nearby_review(store,run_id,threshold):
    """Weak, two-sided context; strong raw anchors in a 120-second window.

    No chained promotion: anchors must be raw single-image scores. Folder labels
    and evaluation labels are never read. This optional feature defaults off.
    """
    store.db.execute('''CREATE TEMP TABLE IF NOT EXISTS person_anchors(source TEXT,parent TEXT,mtime INTEGER,score REAL)''')
    store.db.execute('DELETE FROM person_anchors')
    for r in store.db.execute("SELECT source,mtime,metrics FROM files WHERE seen_run=? AND state IN ('selected','moved')",(run_id,)):
        from pathlib import Path
        score=json.loads(r['metrics']).get('people_score',0)
        if score>=max(.85,threshold):store.db.execute('INSERT INTO person_anchors VALUES(?,?,?,?)',(r['source'],str(Path(r['source']).parent),r['mtime'],score))
    store.db.execute('CREATE INDEX IF NOT EXISTS person_anchor_time ON person_anchors(parent,mtime)')
    store.db.execute('CREATE TEMP TABLE IF NOT EXISTS person_exclusions(parent TEXT,mtime INTEGER)')
    store.db.execute('DELETE FROM person_exclusions')
    from pathlib import Path
    for r in store.db.execute("SELECT source,mtime,metrics FROM files WHERE seen_run=? AND state='rejected' AND reason='not_people'",(run_id,)):
        m=json.loads(r['metrics'])
        if m.get('people_score',1)<=.15 and m.get('person_category') in ('crowd_environment','other'):
            store.db.execute('INSERT INTO person_exclusions VALUES(?,?)',(str(Path(r['source']).parent),r['mtime']))
    store.db.execute('CREATE INDEX IF NOT EXISTS person_exclusion_time ON person_exclusions(parent,mtime)')
    demoted=0
    for r in store.db.execute("SELECT * FROM files WHERE seen_run=? AND state='selected' ORDER BY source",(run_id,)):
        metrics=json.loads(r['metrics']);score=metrics.get('people_score',0)
        if score>.95 or metrics.get('person_category') not in ('depicted_person','performance'):continue
        bounds=(str(Path(r['source']).parent),r['mtime']-120_000_000_000,r['mtime']+120_000_000_000)
        negatives=store.db.execute('SELECT COUNT(*) FROM person_exclusions WHERE parent=? AND mtime BETWEEN ? AND ?',bounds).fetchone()[0]
        positives=store.db.execute('SELECT COUNT(*) FROM person_anchors WHERE parent=? AND mtime BETWEEN ? AND ?',bounds).fetchone()[0]
        # Include neither the candidate nor its weak context in new anchors.
        positives-=int(score>=max(.85,threshold))
        if negatives>=3 and negatives>=3*max(1,positives):
            metrics.update(decision_basis='同目录时间邻域排除辅助',nearby_exclusions=negatives)
            store.decision(r['source'],'rejected','not_people',metrics,r['thumb'],r['pixel_digest']);demoted+=1
    store.db.execute("CREATE TEMP TABLE IF NOT EXISTS person_context_stats(demoted INTEGER)")
    store.db.execute('DELETE FROM person_context_stats');store.db.execute('INSERT INTO person_context_stats VALUES(?)',(demoted,))
    # Context-rejected images must not support a promotion in the second pass.
    # The exclusion pass itself used the unchanged raw anchor snapshot.
    store.db.execute("DELETE FROM person_anchors WHERE source IN (SELECT source FROM files WHERE seen_run=? AND state='rejected')",(run_id,))
    record=store.db.execute('SELECT config FROM runs WHERE id=?',(run_id,)).fetchone()
    verify_cutoff=json.loads(record[0]).get('people_verify_threshold',.8) if record else .8
    promoted=0
    from pathlib import Path
    for r in store.db.execute("SELECT * FROM files WHERE seen_run=? AND state='rejected' AND reason='not_people'",(run_id,)):
        metrics=json.loads(r['metrics']);score=metrics.get('people_score',0)
        # Semantic failures are explicit vetoes, not an embedding threshold
        # ambiguity. Context must never override a rejected verification or
        # scene guard, nor an unsupported format.
        semantic_scores=metrics.get('category_scores',{})
        if semantic_scores.get('semantic_model'):
            if semantic_scores.get('verification_rejected') or metrics.get('verification_score') is None:continue
            if not semantic_scores.get('art_check') or semantic_scores.get('illustration_rejected'):continue
            # A stricter primary cutoff may be supported by neighbours, but
            # the independent body check still has to meet its own cutoff.
            if metrics.get('verification_score',0)<verify_cutoff:continue
        if score<max(.5,threshold-.05):continue
        if metrics.get('decision_basis')=='同目录时间邻域排除辅助':continue
        if metrics.get('person_category') and metrics['person_category'] not in TARGETS:continue
        anchors=store.db.execute('SELECT COUNT(*) FROM person_anchors WHERE parent=? AND mtime BETWEEN ? AND ?',
                  (str(Path(r['source']).parent),r['mtime']-120_000_000_000,r['mtime']+120_000_000_000)).fetchone()[0]
        if anchors>=3:
            negative_neighbors=store.db.execute('SELECT COUNT(*) FROM person_exclusions WHERE parent=? AND mtime BETWEEN ? AND ?',
                  (str(Path(r['source']).parent),r['mtime']-120_000_000_000,r['mtime']+120_000_000_000)).fetchone()[0]
            if negative_neighbors>=anchors:continue
            metrics.update(decision_basis='同目录时间邻域辅助',nearby_anchors=anchors)
            store.decision(r['source'],'selected','selected',metrics,r['thumb'],r['pixel_digest']);promoted+=1
    store.db.commit();return promoted
