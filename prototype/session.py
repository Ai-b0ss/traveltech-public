"""Revision-checked local refinements. No supplier mutation or booking undo."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'base'))
from dataclasses import dataclass,replace
from travel_core import Request,Engine,InputError

ALLOWED_PATCH={'budget','breakfast','free_cancel_until','city','country','checkin','checkout','rooms','currency','allowed_countries','payment_method'}
DATA_CONTEXT={'city','country','checkin','checkout','rooms'}
class Conflict(ValueError):pass

@dataclass(frozen=True)
class State:
    request:Request
    revision:int=0
    selected_offer:tuple|None=None

@dataclass
class Session:
    state:State
    history:list=None
    def __post_init__(self):
        self.state.request.validate();self.history=[]
        if type(self.state.revision) is not int or self.state.revision<0:raise InputError('INVALID_REVISION')
    def preview(self,patch,expected_revision):
        if type(expected_revision) is not int or expected_revision!=self.state.revision:raise Conflict('STALE_REVISION')
        if not isinstance(patch,dict) or set(patch)-ALLOWED_PATCH:raise InputError('PATCH_FIELDS_NOT_ALLOWED')
        request=replace(self.state.request,**patch);request.validate()
        changes={k:{'before':getattr(self.state.request,k),'after':v} for k,v in patch.items() if getattr(self.state.request,k)!=v}
        return {'request':request,'changes':changes,'base_revision':self.state.revision,'source_refresh_required':bool(set(changes)&DATA_CONTEXT)}
    def apply(self,preview):
        if not isinstance(preview,dict) or type(preview.get('base_revision')) is not int or preview.get('base_revision')!=self.state.revision:raise Conflict('STALE_REVISION')
        # Recompute from whitelisted changes. Do not trust arbitrary request
        # replacement or caller-supplied refresh flag in a preview object.
        recomputed=self.preview({k:v['after'] for k,v in preview['changes'].items()},self.state.revision)
        if preview['request']!=recomputed['request']:raise InputError('PREVIEW_TAMPERED')
        if not recomputed['changes']:return self.state
        self.history.append(self.state)
        self.state=State(recomputed['request'],self.state.revision+1,self.state.selected_offer)
        return self.state
    def undo(self,expected_revision):
        if type(expected_revision) is not int or expected_revision!=self.state.revision:raise Conflict('STALE_REVISION')
        if not self.history:raise Conflict('NO_PREVIOUS_STATE')
        old=self.history.pop();self.state=State(old.request,self.state.revision+1,old.selected_offer);return self.state
    def recheck(self,engine,now):
        result=engine.search(self.state.request,now)
        selected=self.state.selected_offer
        matching=[d for d in result.get('decisions',[]) if d.identity==selected]
        selected_status='NONE_SELECTED' if selected is None else matching[0].status if matching else 'SELECTED_OFFER_NOT_IN_RESULT'
        return {'revision':self.state.revision,'request':self.state.request,'result':result,'selected_offer_status':selected_status,'supplier_actions':0}
