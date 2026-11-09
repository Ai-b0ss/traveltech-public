"""Checks supplied evidence records, not natural-language truth or image content."""
from dataclasses import dataclass
from datetime import datetime,timedelta
import hashlib,re
SUBJECTIVE_ASPECTS={'quiet','cleanliness','comfort','service_quality','value_for_money'}

@dataclass(frozen=True)
class Evidence:
    evidence_id:str
    hotel_id:str
    aspect:str
    value:bool
    kind:str # RATE, PROPERTY, REVIEW, PHOTO
    observed_at:datetime
    source_ref:str
    room_id:str|None=None
    rate_id:str|None=None
    text:str=''
    stay_verified:bool=False
    content_date:datetime|None=None

def verify_claim(records,*,hotel_id,aspect,now,room_id=None,rate_id=None,max_age=timedelta(days=90)):
    if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:raise ValueError('AWARE_NOW_REQUIRED')
    if not isinstance(max_age,timedelta) or max_age<timedelta(0):raise ValueError('INVALID_MAX_AGE')
    if not isinstance(records,(list,tuple)) or len(records)>10000:raise ValueError('INVALID_EVIDENCE_COLLECTION')
    accepted=[];excluded=[];seen=set()
    for e in records:
        if not isinstance(e,Evidence) or type(e.value) is not bool or e.kind not in ('RATE','PROPERTY','REVIEW','PHOTO') or type(e.stay_verified) is not bool:raise ValueError('INVALID_EVIDENCE')
        if any(not isinstance(x,str) or not x.strip() for x in (e.evidence_id,e.source_ref,e.hotel_id,e.aspect)):raise ValueError('EVIDENCE_PROVENANCE_REQUIRED')
        if not isinstance(e.text,str):raise ValueError('INVALID_EVIDENCE_TEXT')
        try:e.text.encode('utf-8')
        except UnicodeError as exc:raise ValueError('INVALID_EVIDENCE_TEXT') from exc
        if not isinstance(e.observed_at,datetime) or e.observed_at.tzinfo is None or e.observed_at.utcoffset() is None:raise ValueError('AWARE_OBSERVATION_REQUIRED')
        reason=None
        if e.hotel_id!=hotel_id or e.aspect!=aspect:reason='OTHER_OBJECT_OR_ASPECT'
        elif room_id is not None and e.room_id!=room_id:reason='ROOM_NOT_BOUND'
        elif rate_id is not None and e.rate_id!=rate_id:reason='RATE_NOT_BOUND'
        elif e.observed_at>now or now-e.observed_at>max_age:reason='STALE_OR_FUTURE'
        if reason is None and e.kind=='REVIEW':
            if not isinstance(e.content_date,datetime) or e.content_date.tzinfo is None or e.content_date.utcoffset() is None:reason='REVIEW_DATE_UNKNOWN'
            elif e.content_date>now or now-e.content_date>max_age:reason='REVIEW_TEXT_STALE_OR_FUTURE'
        # Only identical copied review texts are deduplicated; similar genuine
        # experiences must not be discarded simply because they are negative.
        key=('review',hashlib.sha256(re.sub(r'\s+',' ',e.text.strip().casefold()).encode()).hexdigest()) if e.kind=='REVIEW' and e.text.strip() else ('record',e.evidence_id,e.source_ref)
        if reason is None and key in seen:reason='EXACT_DUPLICATE'
        if reason:excluded.append({'id':e.evidence_id,'reason':reason});continue
        seen.add(key);accepted.append(e)
    facts=[e for e in accepted if e.kind in ('RATE','PROPERTY')]
    positive=[e for e in facts if e.value];negative=[e for e in facts if not e.value]
    opinions=[e for e in accepted if e.kind=='REVIEW'];photos=[e for e in accepted if e.kind=='PHOTO']
    if aspect in SUBJECTIVE_ASPECTS and (facts or opinions):status='SUBJECTIVE_REPORTS_ONLY'
    elif positive and negative:status='CONFLICTING_FACTS'
    elif positive:status='SUPPORTED_FACT'
    elif negative:status='CONTRADICTED_FACT'
    elif opinions:status='SUBJECTIVE_REPORTS_ONLY'
    elif photos:status='VISUAL_OBSERVATION_ONLY'
    else:status='UNKNOWN'
    subjective=aspect in SUBJECTIVE_ASPECTS
    return {'status':status,'aspect':aspect,'supporting_fact_ids':[] if subjective else [e.evidence_id for e in positive],'contradicting_fact_ids':[] if subjective else [e.evidence_id for e in negative],'description_support_count':len(positive) if subjective else 0,'description_contra_count':len(negative) if subjective else 0,'review_support_count':sum(e.value for e in opinions),'review_contra_count':sum(not e.value for e in opinions),'verified_stay_reviews':sum(e.stay_verified for e in opinions),'photo_ids':[e.evidence_id for e in photos],'accepted_sources':[{'id':e.evidence_id,'source':e.source_ref,'observed_at':e.observed_at.isoformat()} for e in accepted],'excluded':excluded,'negative_reviews_discarded_for_sentiment':False,'natural_language_or_image_extraction_executed':False}

def explain(receipt):
    status=receipt['status']
    if status=='SUPPORTED_FACT':return 'Утверждение подтверждено указанными структурированными данными.'
    if status=='CONTRADICTED_FACT':return 'Утверждение противоречит указанным структурированным данным.'
    if status=='CONFLICTING_FACTS':return 'В источниках есть противоречие; свойство требует проверки.'
    if status=='SUBJECTIVE_REPORTS_ONLY':return f"Есть субъективные сведения: отзывы за — {receipt['review_support_count']}, против — {receipt['review_contra_count']}; положительные описания — {receipt['description_support_count']}, отрицательные — {receipt['description_contra_count']}. Это не гарантия свойства."
    if status=='VISUAL_OBSERVATION_ONLY':return 'Есть визуальное наблюдение; оно не доказывает актуальное свойство или услугу.'
    return 'Подходящих актуальных оснований не найдено.'
